"""Utility functions for AH tests."""

import json
import logging
import os
import random
import re
import requests
import shutil
import string
import subprocess
import tarfile
import tempfile
import time
import uuid

from contextlib import contextmanager
from subprocess import PIPE, run
from urllib.parse import urljoin
from urllib.parse import urlparse

from ansible import context
from ansible.galaxy.api import GalaxyAPI, GalaxyError
from ansible.galaxy.token import BasicAuthToken, GalaxyToken, KeycloakToken
from orionutils.generator import build_collection as _build_collection
from orionutils.generator import randstr

from galaxy_ng.tests.integration.constants import SLEEP_SECONDS_POLLING

from .constants import USERNAME_PUBLISHER

logger = logging.getLogger(__name__)


class TaskWaitingTimeout(Exception):
    pass


class CapturingGalaxyError(Exception):
    def __init__(self, http_error, message, http_code=None):
        self.http_error = http_error
        self.message = message
        self.http_code = http_code


class CollectionInspector:

    """ Easy shim to look at tarballs or installed collections """

    def __init__(self, tarball=None, directory=None):
        self.tarball = tarball
        self.directory = directory
        self.manifest = None
        self._extract_path = None
        self._enumerate()

    def _enumerate(self):
        if self.tarball:
            self._extract_path = tempfile.mkdtemp(prefix='collection-extract-')
            cmd = f'cd {self._extract_path}; tar xzvf {self.filename}'
            subprocess.run(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
        else:
            self._extract_path = self.directory

        with open(os.path.join(self._extract_path, 'MANIFEST.json'), 'r') as f:
            self.manifest = json.loads(f.read())

        if self.tarball:
            shutil.rmtree(self._extract_path)

    @property
    def namespace(self):
        if self.manifest is None:
            return None
        return self.manifest['collection_info']['namespace']

    @property
    def name(self):
        if self.manifest is None:
            return None
        return self.manifest['collection_info']['name']

    @property
    def tags(self):
        if self.manifest is None:
            return None
        return self.manifest['collection_info']['tags']

    @property
    def version(self):
        if self.manifest is None:
            return None
        return self.manifest['collection_info']['version']


def get_client(config, require_auth=True, request_token=True, headers=None):
    """Get an API client given a role."""
    headers = headers or {}
    server = config["url"]
    assert "200" not in server
    auth_url = config.get("auth_url")

    # force the galaxy client lib to think the ignore certs kwarg was used
    # NOTE: this does not work with 2.12+
    context.CLIARGS = {"ignore_certs": True}

    # request token implies that upstream test wants to use authentication.
    # however, some tests need to auth but send request_token=False, so this
    # kwarg is poorly named and confusing.
    token = config.get("token") or None
    if request_token:
        if token:
            # keycloak must have a unique auth url ...
            if auth_url:
                token = KeycloakToken(config["token"], auth_url=auth_url)
            else:
                token = GalaxyToken(config["token"])
        else:
            token = BasicAuthToken(config["username"], config["password"])
    else:
        if require_auth:
            token = BasicAuthToken(config["username"], config["password"])
        else:
            token = None
    client = GalaxyAPI(None, "automation_hub", url=server, token=token)

    # Fix for 2.12+
    client.validate_certs = False

    # make an api call with the upstream galaxy client lib from ansible core
    def request(url, *args, **kwargs):
        url = urljoin(server, url)
        req_headers = dict(headers)
        kwargs_headers = kwargs.get('headers') or {}
        kwargs_headers_keys = list(kwargs_headers.keys())
        kwargs_headers_keys = [x.lower() for x in list(kwargs_headers.keys())]

        if isinstance(kwargs.get("args"), dict):
            kwargs["args"] = json.dumps(kwargs["args"])

        # Always send content-type
        if 'content-type' in kwargs_headers_keys:
            for k, v in headers.items():
                if k.lower() == 'content-type':
                    req_headers.pop(k, None)
        else:
            req_headers["Content-Type"] = "application/json"

        if req_headers:
            if "headers" in kwargs:
                kwargs["headers"].update(req_headers)
            else:
                kwargs["headers"] = req_headers

        return client._call_galaxy(url, *args, **kwargs)

    request.config = config
    return request


def upload_artifact(
    config, client, artifact, hash=True, no_filename=False, no_file=False, use_distribution=True
):
    """
    Publishes a collection to a Galaxy server and returns the import task URI.

    :param collection_path: The path to the collection tarball to publish.
    :return: The import task URI that contains the import results.
    """
    collection_path = artifact.filename
    with open(collection_path, "rb") as collection_tar:
        data = collection_tar.read()

    def to_bytes(s, errors=None):
        return s.encode("utf8")

    boundary = "--------------------------%s" % uuid.uuid4().hex
    file_name = os.path.basename(collection_path)
    part_boundary = b"--" + to_bytes(boundary, errors="surrogate_or_strict")

    from ansible.galaxy.api import _urljoin
    from ansible.utils.hashing import secure_hash_s

    form = []

    if hash:
        if isinstance(hash, bytes):
            b_hash = hash
        else:
            from hashlib import sha256

            b_hash = to_bytes(secure_hash_s(data, sha256), errors="surrogate_or_strict")
        form.extend([part_boundary, b'Content-Disposition: form-data; name="sha256"', b_hash])

    if not no_file:
        if no_filename:
            form.extend(
                [
                    part_boundary,
                    b'Content-Disposition: file; name="file"',
                    b"Content-Type: application/octet-stream",
                ]
            )
        else:
            form.extend(
                [
                    part_boundary,
                    b'Content-Disposition: file; name="file"; filename="%s"' % to_bytes(file_name),
                    b"Content-Type: application/octet-stream",
                ]
            )
    else:
        form.append(part_boundary)

    form.extend([b"", data, b"%s--" % part_boundary])

    data = b"\r\n".join(form)

    headers = {
        "Content-type": "multipart/form-data; boundary=%s" % boundary,
        "Content-length": len(data),
    }

    n_url = ""
    if use_distribution:
        n_url = (
            _urljoin(
                config["url"],
                "content",
                f"inbound-{artifact.namespace}",
                "v3",
                "artifacts",
                "collections",
            )
            + "/"
        )
    else:
        n_url = _urljoin(config["url"], "v3", "artifacts", "collections") + "/"

    resp = client(n_url, args=data, headers=headers, method="POST", auth_required=True)

    return resp


def wait_for_task(api_client, resp, timeout=300):
    ready = False
    url = urljoin(api_client.config["url"], resp["task"])
    wait_until = time.time() + timeout
    while not ready:
        if wait_until < time.time():
            raise TaskWaitingTimeout()
        try:
            resp = api_client(url)
        except GalaxyError as e:
            if "500" not in str(e):
                raise
        else:
            ready = resp["state"] not in ("running", "waiting")
        time.sleep(SLEEP_SECONDS_POLLING)
    return resp


def wait_for_url(api_client, url, timeout_sec=30):
    """Wait until url stops returning a 404."""
    ready = False
    res = None
    wait_until = time.time() + timeout_sec
    while not ready:
        if wait_until < time.time():
            raise TaskWaitingTimeout()
        try:
            res = api_client(url, method="GET")
        except GalaxyError as e:
            if "404" not in str(e):
                raise
            time.sleep(0.5)
        else:
            ready = True
    return res


@contextmanager
def modify_artifact(artifact):
    filename = artifact.filename
    with tempfile.TemporaryDirectory() as dirpath:
        # unpack
        tf = tarfile.open(filename)
        tf.extractall(dirpath)

        try:
            yield dirpath

        finally:
            # re-pack
            tf = tarfile.open(filename, "w:gz")
            for name in os.listdir(dirpath):
                tf.add(os.path.join(dirpath, name), name)
            tf.close()


def uuid4():
    """Return a random UUID4 as a string."""
    return str(uuid.uuid4())


def ansible_galaxy(
    command,
    check_retcode=0,
    server="automation_hub",
    ansible_config=None,
    cleanup=True
):

    tdir = tempfile.mkdtemp(prefix='ansible-galaxy-testing-')
    if not os.path.exists(tdir):
        os.makedirs(tdir)
    cfgfile = os.path.join(tdir, 'ansible.cfg')
    with open(cfgfile, 'w') as f:
        f.write('[galaxy]\n')
        f.write(f'server_list = {server}\n')
        f.write('\n')
        f.write(f'[galaxy_server.{server}]\n')
        f.write(f"url={ansible_config.get('url')}\n")
        if ansible_config.get('auth_url'):
            f.write(f"auth_url={ansible_config.get('auth_url')}\n")
        f.write('validate_certs=False\n')
        f.write(f"username={ansible_config.get('username')}\n")
        f.write(f"password={ansible_config.get('password')}\n")
        if ansible_config.get('token'):
            f.write(f"token={ansible_config.get('token')}\n")

    command_string = f"ansible-galaxy -vvv {command} --server={server} --ignore-certs"
    p = run(command_string, cwd=tdir, shell=True, stdout=PIPE, stderr=PIPE, env=os.environ)
    logger.debug(f"RUN {command_string}")
    logger.debug("STDOUT---")
    for line in p.stdout.decode("utf8").split("\n"):
        logger.debug(re.sub("(.\x08)+", "...", line))
    logger.debug("STDERR---")
    for line in p.stderr.decode("utf8").split("\n"):
        logger.debug(re.sub("(.\x08)+", "...", line))
    if check_retcode is not False:
        assert p.returncode == check_retcode, p.stderr.decode("utf8")
    if cleanup:
        shutil.rmtree(tdir)
    return p


def get_collections_namespace_path(namespace):
    """Get collections namespace path."""
    return os.path.expanduser(f"~/.ansible/collections/ansible_collections/{namespace}/")


def get_collection_full_path(namespace, collection_name):
    """Get collections full path."""
    return os.path.join(get_collections_namespace_path(namespace), collection_name)


def set_certification(client, collection):
    """Moves a collection from the `staging` to the `published` repository.

    For use in instances that use repository-based certification and that
    do not have auto-certification enabled.
    """
    if client.config["use_move_endpoint"]:
        url = (
            f"v3/collections/{collection.namespace}/{collection.name}/versions/"
            f"{collection.version}/move/staging/published/"
        )

        job_tasks = client(url, method="POST", args=b"{}")
        assert 'copy_task_id' in job_tasks
        assert 'curate_all_synclist_repository_task_id' in job_tasks
        assert 'remove_task_id' in job_tasks

        # wait for each unique task to finish ...
        for key in ['copy_task_id', 'curate_all_synclist_repository_task_id', 'remove_task_id']:
            task_id = job_tasks.get(key)

            # curate is null sometimes? ...
            if task_id is None:
                continue

            # The task_id is not a url, so it has to be assembled from known data ...
            # http://.../api/automation-hub/pulp/api/v3/tasks/8be0b9b6-71d6-4214-8427-2ecf81818ed4/
            ds = {
                'task': f"{client.config['url']}/pulp/api/v3/tasks/{task_id}"
            }
            task_result = wait_for_task(client, ds)
            assert task_result['state'] == 'completed', task_result

        # callers expect response as part of this method, ensure artifact is there
        dest_url = (
            "content/published/v3/plugin/ansible/content/published/collections/index/"
            f"{collection.namespace}/{collection.name}/versions/{collection.version}/"
        )
        return wait_for_url(client, dest_url)


def generate_namespace(exclude=None):
    """ Create a valid random namespace string """

    # This should be a list of pre-existing namespaces
    if exclude is None:
        exclude = []

    def is_valid(ns):
        """ Assert namespace meets backend requirements """
        if ns is None:
            return False
        if ns in exclude:
            return False
        if len(namespace) < 3:
            return False
        if len(namespace) > 64:
            return False
        for char in namespace:
            if char not in string.ascii_lowercase + string.digits:
                return False

        return True

    namespace = None
    while not is_valid(namespace):
        namespace = ''
        namespace += random.choice(string.ascii_lowercase)
        for x in range(0, random.choice(range(3, 63))):
            namespace += random.choice(string.ascii_lowercase + string.digits + '_')

    return namespace


def get_all_namespaces(api_client=None, api_version='v3'):
    """ Create a list of namespaces visible to the client """

    assert api_client is not None, "api_client is a required param"
    namespaces = []
    next_page = f'/api/automation-hub/{api_version}/namespaces/'
    while next_page:
        resp = api_client(next_page)
        namespaces.extend(resp['data'])
        next_page = resp.get('links', {}).get('next')
    return namespaces


def generate_unused_namespace(api_client=None, api_version='v3'):
    """ Make a random namespace string that does not exist """

    assert api_client is not None, "api_client is a required param"
    existing = get_all_namespaces(api_client=api_client, api_version=api_version)
    existing = dict((x['name'], x) for x in existing)
    return generate_namespace(exclude=list(existing.keys()))


def create_unused_namespace(api_client=None):
    """ Make a namespace for testing """

    assert api_client is not None, "api_client is a required param"
    ns = generate_unused_namespace(api_client=api_client)
    payload = {'name': ns, 'groups': []}
    api_client('/api/automation-hub/v3/namespaces/', args=payload, method='POST')
    return ns


def get_all_collections_by_repo(api_client=None):
    """ Return a dict of each repo and their collections """
    assert api_client is not None, "api_client is a required param"
    collections = {
        'staging': {},
        'published': {}
    }
    for repo in collections.keys():
        next_page = f'/api/automation-hub/_ui/v1/collection-versions/?repository={repo}'
        while next_page:
            resp = api_client(next_page)
            for _collection in resp['data']:
                key = (_collection['namespace'], _collection['name'], _collection['version'])
                collections[repo][key] = _collection
            next_page = resp.get('links', {}).get('next')
    return collections


def get_all_repository_collection_versions(api_client):
    """ Return a dict of each repo and their collection versions """

    repositories = [
        'staging',
        'published'
    ]

    collections = []
    for repo in repositories:
        next_page = f'/api/automation-hub/content/{repo}/v3/collections/'
        while next_page:
            resp = api_client(next_page)
            collections.extend(resp['data'])
            next_page = resp.get('links', {}).get('next')

    collection_versions = []
    for collection in collections:
        next_page = collection['versions_url']
        while next_page:
            resp = api_client(next_page)
            for cv in resp['data']:
                cv['namespace'] = collection['namespace']
                cv['name'] = collection['name']
                if 'staging' in cv['href']:
                    cv['repository'] = 'staging'
                else:
                    cv['repository'] = 'published'
                collection_versions.append(cv)
            next_page = resp.get('links', {}).get('next')

    rcv = dict(
        (
            (x['repository'], x['namespace'], x['name'], x['version']),
            x
        )
        for x in collection_versions
    )
    return rcv


def build_collection(
    base=None,
    config=None,
    filename=None,
    key=None,
    pre_build=None,
    extra_files=None,
    namespace=None,
    name=None,
    tags=None,
    version=None,
    dependencies=None
):

    if base is None:
        base = "skeleton"

    if config is None:
        config = {
            "namespace": None,
            "name": None,
            "tags": []
        }

    if namespace is not None:
        config['namespace'] = namespace
    else:
        config['namespace'] = USERNAME_PUBLISHER

    if name is not None:
        config['name'] = name
    else:
        config['name'] = randstr()

    if version is not None:
        config['version'] = version

    if dependencies is not None:
        config['dependencies'] = dependencies

    if tags is not None:
        config['tags'] = tags

    # workaround for cloud importer config
    if 'tools' not in config['tags']:
        config['tags'].append('tools')

    return _build_collection(
        base,
        config=config,
        filename=filename,
        key=key,
        pre_build=pre_build,
        extra_files=extra_files
    )


def is_docker_installed():
    return shutil.which("docker") is not None


class UIClient:

    """ An HTTP client to mimic the UI """

    _rs = None

    def __init__(self, username=None, password=None, baseurl=None, authurl=None, config=None):
        self.username = username
        self.password = password
        self.baseurl = baseurl
        self.authurl = authurl
        self.config = config

        # default to config settings ...
        if self.config is not None and not self.username:
            self.username = self.config.get('username')
        if self.config is not None and not self.password:
            self.password = self.config.get('password')
        if self.config is not None and not self.baseurl:
            self.baseurl = self.config.get('url')
        if self.config is not None and not self.authurl:
            self.authurl = self.config.get('auth_url')

        self.login_url = self.baseurl + '_ui/v1/auth/login/'
        self.logout_url = self.baseurl + '_ui/v1/auth/logout/'

    @property
    def cookies(self):
        return dict(self._rs.cookies)

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, a, b, c):
        self.logout()

    def login(self):
        self._rs = requests.Session()

        # GET the page to acquire the csrftoken
        self._rs.get(self.login_url)

        # parse the cookies
        cookies = self.cookies

        # now POST the credentials
        pheaders = {
            'Cookie': f"csrftoken={cookies['csrftoken']}",
            'X-CSRFToken': cookies['csrftoken']
        }
        self._rs.post(
            self.login_url,
            headers=pheaders,
            json={'username': self.username, 'password': self.password}
        )

    def logout(self, expected_code=None):

        if self._rs is None:
            raise Exception('client is not authenticated')

        # POST to the logout url with the cookie and sesionid
        cookies = self.cookies
        pheaders = {
            'Content-Type': 'application/json',
            'Cookie': f"csrftoken={cookies['csrftoken']}; sessionid={cookies['sessionid']}",
            'X-CSRFToken': cookies['csrftoken']
        }
        res = self._rs.post(self.logout_url, json={}, headers=pheaders)

        if expected_code is not None:
            if res.status_code != expected_code:
                raise Exception(f'logout status code was not {expected_code}')

    def get(self, relative_url: str = None, absolute_url: str = None) -> requests.models.Response:

        pheaders = {
            'Accept': 'application/json',
        }

        # send cookies whenever possible ...
        if self.cookies is not None:
            cookie = []
            if self.cookies.get('csrftoken'):
                cookie.append(f"csrftoken={self.cookies['csrftoken']}")
            if self.cookies.get('sessionid'):
                cookie.append(f"sessionid={self.cookies['sessionid']}")
            pheaders['Cookie'] = '; '.join(cookie)

        this_url = None
        if absolute_url:
            uri = urlparse(self.baseurl)
            this_url = f"{uri.scheme}://{uri.netloc}{absolute_url}"
        else:
            this_url = self.baseurl + relative_url

        # get the response
        resp = self._rs.get(this_url, headers=pheaders)
        return resp

    def post(self, relative_url: str, payload: dict) -> requests.models.Response:
        pheaders = {
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }

        # send cookies whenever possible ...
        if self.cookies is not None:
            cookie = []
            if self.cookies.get('csrftoken'):
                pheaders['X-CSRFToken'] = self.cookies['csrftoken']
                cookie.append(f"csrftoken={self.cookies['csrftoken']}")
            if self.cookies.get('sessionid'):
                cookie.append(f"sessionid={self.cookies['sessionid']}")
            pheaders['Cookie'] = '; '.join(cookie)

        # get the response
        resp = self._rs.post(self.baseurl + relative_url, json=payload, headers=pheaders)
        return resp

    def delete(self, relative_url: str) -> requests.models.Response:
        pheaders = {
            'Accept': 'application/json',
        }

        # send cookies whenever possible ...
        if self.cookies is not None:
            cookie = []
            if self.cookies.get('csrftoken'):
                pheaders['X-CSRFToken'] = self.cookies['csrftoken']
                cookie.append(f"csrftoken={self.cookies['csrftoken']}")
            if self.cookies.get('sessionid'):
                cookie.append(f"sessionid={self.cookies['sessionid']}")
            pheaders['Cookie'] = '; '.join(cookie)

        # get the response
        resp = self._rs.delete(self.baseurl + relative_url, headers=pheaders)
        return resp
