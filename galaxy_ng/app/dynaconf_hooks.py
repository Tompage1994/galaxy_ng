import os
from typing import Any, Dict, List
from dynaconf import Dynaconf, Validator


def post(settings: Dynaconf) -> Dict[str, Any]:
    """The dynaconf post hook is called after all the settings are loaded and set.

    Post hook is necessary when a setting key depends conditionally on a previouslys et variable.

    settings: A read-only copy of the django.conf.settings
    returns: a dictionary to be merged to django.conf.settings

    NOTES:
        Feature flags must be loaded directly on `app/api/ui/views/feature_flags.py` view.
    """
    data = {"dynaconf_merge": False}
    # existing keys will be merged if dynaconf_merge is set to True
    # here it is set to false, so it allows each value to be individually marked as a merge.

    data.update(configure_logging(settings))
    data.update(configure_keycloak(settings))
    data.update(configure_cors(settings))
    data.update(configure_pulp_ansible(settings))
    data.update(configure_authentication_classes(settings))
    data.update(configure_password_validators(settings))
    data.update(configure_api_base_path(settings))

    validate(settings)
    return data


def configure_keycloak(settings: Dynaconf) -> Dict[str, Any]:
    """Configure keycloak settings for galaxy.

    This function returns a dictionary that will be merged to the settings.
    """

    data = {}

    # Obtain values for Social Auth
    SOCIAL_AUTH_KEYCLOAK_KEY = settings.get("SOCIAL_AUTH_KEYCLOAK_KEY", default=None)
    SOCIAL_AUTH_KEYCLOAK_SECRET = settings.get("SOCIAL_AUTH_KEYCLOAK_SECRET", default=None)
    SOCIAL_AUTH_KEYCLOAK_PUBLIC_KEY = settings.get("SOCIAL_AUTH_KEYCLOAK_PUBLIC_KEY", default=None)
    KEYCLOAK_PROTOCOL = settings.get("KEYCLOAK_PROTOCOL", default=None)
    KEYCLOAK_HOST = settings.get("KEYCLOAK_HOST", default=None)
    KEYCLOAK_PORT = settings.get("KEYCLOAK_PORT", default=None)
    KEYCLOAK_REALM = settings.get("KEYCLOAK_REALM", default=None)

    # Add settings if Social Auth values are provided
    if all(
        [
            SOCIAL_AUTH_KEYCLOAK_KEY,
            SOCIAL_AUTH_KEYCLOAK_SECRET,
            SOCIAL_AUTH_KEYCLOAK_PUBLIC_KEY,
            KEYCLOAK_HOST,
            KEYCLOAK_PORT,
            KEYCLOAK_REALM,
        ]
    ):
        data["KEYCLOAK_ADMIN_ROLE"] = settings.get("KEYCLOAK_ADMIN_ROLE", default="hubadmin")
        data["KEYCLOAK_GROUP_TOKEN_CLAIM"] = settings.get(
            "KEYCLOAK_GROUP_TOKEN_CLAIM", default="group"
        )
        data["KEYCLOAK_ROLE_TOKEN_CLAIM"] = settings.get(
            "KEYCLOAK_GROUP_TOKEN_CLAIM", default="client_roles"
        )
        data["KEYCLOAK_HOST_LOOPBACK"] = settings.get("KEYCLOAK_HOST_LOOPBACK", default=None)
        data["KEYCLOAK_URL"] = f"{KEYCLOAK_PROTOCOL}://{KEYCLOAK_HOST}:{KEYCLOAK_PORT}"
        auth_url_str = "{keycloak}/auth/realms/{realm}/protocol/openid-connect/auth/"
        data["SOCIAL_AUTH_KEYCLOAK_AUTHORIZATION_URL"] = auth_url_str.format(
            keycloak=data["KEYCLOAK_URL"], realm=KEYCLOAK_REALM
        )
        if data["KEYCLOAK_HOST_LOOPBACK"]:
            loopback_url = "{protocol}://{host}:{port}".format(
                protocol=KEYCLOAK_PROTOCOL, host=data["KEYCLOAK_HOST_LOOPBACK"], port=KEYCLOAK_PORT
            )
            data["SOCIAL_AUTH_KEYCLOAK_AUTHORIZATION_URL"] = auth_url_str.format(
                keycloak=loopback_url, realm=KEYCLOAK_REALM
            )

        data[
            "SOCIAL_AUTH_KEYCLOAK_ACCESS_TOKEN_URL"
        ] = f"{data['KEYCLOAK_URL']}/auth/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token/"

        data["SOCIAL_AUTH_LOGIN_REDIRECT_URL"] = settings.get(
            "SOCIAL_AUTH_LOGIN_REDIRECT_URL", default="/ui/"
        )
        data["SOCIAL_AUTH_POSTGRES_JSONFIELD"] = True
        data["SOCIAL_AUTH_JSONFIELD_CUSTOM"] = "django.contrib.postgres.fields.JSONField"
        data["SOCIAL_AUTH_URL_NAMESPACE"] = "social"
        data["SOCIAL_AUTH_KEYCLOAK_EXTRA_DATA"] = [
            ("refresh_token", "refresh_token"),
            (data["KEYCLOAK_ROLE_TOKEN_CLAIM"], data["KEYCLOAK_ROLE_TOKEN_CLAIM"]),
        ]

        data["SOCIAL_AUTH_PIPELINE"] = (
            "social_core.pipeline.social_auth.social_details",
            "social_core.pipeline.social_auth.social_uid",
            "social_core.pipeline.social_auth.social_user",
            "social_core.pipeline.user.get_username",
            "social_core.pipeline.social_auth.associate_by_email",
            "social_core.pipeline.user.create_user",
            "social_core.pipeline.social_auth.associate_user",
            "social_core.pipeline.social_auth.load_extra_data",
            "social_core.pipeline.user.user_details",
            "galaxy_ng.app.pipelines.user_role",
            "galaxy_ng.app.pipelines.user_group",
        )

        # Set external authentication feature flag
        # data["GALAXY_FEATURE_FLAGS"] = {'external_authentication': True, "dynaconf_merge": True}
        # The next have the same effect ^
        data["GALAXY_FEATURE_FLAGS__external_authentication"] = True

        # Add to installed apps
        data["INSTALLED_APPS"] = ["social_django", "dynaconf_merge"]

        # Add to authentication backends
        data["AUTHENTICATION_BACKENDS"] = [
            "social_core.backends.keycloak.KeycloakOAuth2",
            "dynaconf_merge",
        ]

        # Replace AUTH CLASSES
        data["GALAXY_AUTHENTICATION_CLASSES"] = [
            "rest_framework.authentication.SessionAuthentication",
            "galaxy_ng.app.auth.token.ExpiringTokenAuthentication",
            "galaxy_ng.app.auth.keycloak.KeycloakBasicAuth"
        ]

        # Set default to one day expiration
        data["GALAXY_TOKEN_EXPIRATION"] = settings.get("GALAXY_TOKEN_EXPIRATION", 1440)

        # Add to templates
        # Pending dynaconf issue:
        # https://github.com/rochacbruno/dynaconf/issues/299#issuecomment-900616706
        # So we can do a merge of this data.
        data["TEMPLATES"] = [
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [os.path.join(settings.BASE_DIR, "templates")],
                "APP_DIRS": True,
                "OPTIONS": {
                    "context_processors": [
                        # BEGIN: Pulp standard context processors
                        "django.template.context_processors.debug",
                        "django.template.context_processors.request",
                        "django.contrib.auth.context_processors.auth",
                        "django.contrib.messages.context_processors.messages",
                        # END: Pulp standard context processors
                        "social_django.context_processors.backends",
                        "social_django.context_processors.login_redirect",
                    ],
                },
            },
        ]

    return data


def configure_logging(settings: Dynaconf) -> Dict[str, Any]:
    data = {
        "GALAXY_ENABLE_API_ACCESS_LOG": settings.get(
            "GALAXY_ENABLE_API_ACCESS_LOG",
            default=os.getenv("GALAXY_ENABLE_API_ACCESS_LOG", default=False),
        )
    }
    if data["GALAXY_ENABLE_API_ACCESS_LOG"]:
        data["INSTALLED_APPS"] = ["automated_logging", "dynaconf_merge"]
        data["MIDDLEWARE"] = [
            "automated_logging.middleware.AutomatedLoggingMiddleware",
            "dynaconf_merge",
        ]
        data["LOGGING"] = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "automated_logging": {"format": "%(asctime)s: %(levelname)s: %(message)s"},
            },
            "handlers": {
                "automated_logging": {
                    "level": "INFO",
                    "class": "logging.handlers.WatchedFileHandler",
                    "filename": "/var/log/galaxy_api_access.log",
                    "formatter": "automated_logging",
                },
            },
            "loggers": {
                "automated_logging": {
                    "handlers": ["automated_logging"],
                    "level": "INFO",
                    "propagate": False,
                },
            },
            "dynaconf_merge": True,
        }
        data["AUTOMATED_LOGGING"] = {
            "globals": {
                "exclude": {
                    "applications": [
                        "plain:contenttypes",
                        "plain:admin",
                        "plain:basehttp",
                        "glob:session*",
                        "plain:migrations",
                    ]
                }
            },
            "model": {
                "detailed_message": True,
                "exclude": {"applications": [], "fields": [], "models": [], "unknown": False},
                "loglevel": 20,
                "mask": [],
                "max_age": None,
                "performance": False,
                "snapshot": False,
                "user_mirror": False,
            },
            "modules": ["request", "unspecified", "model"],
            "request": {
                "data": {
                    "content_types": ["application/json"],
                    "enabled": [],
                    "ignore": [],
                    "mask": [
                        "ca_cert",
                        "client_cert",
                        "client_key",
                        "email",
                        "password",
                        "proxy_url",
                        "proxy_username",
                        "proxy_password",
                        "token",
                        "username",
                    ],
                    "query": True,
                },
                "exclude": {
                    "applications": [],
                    "methods": [],
                    "status": [],
                    "unknown": False,
                },
                "ip": True,
                "loglevel": 20,
                "max_age": None,
            },
            "unspecified": {
                "exclude": {"applications": [], "files": [], "unknown": False},
                "loglevel": 20,
                "max_age": None,
            },
        }

    return data


def configure_cors(settings: Dynaconf) -> Dict[str, Any]:
    """This adds CORS Middleware, useful to access swagger UI on dev environment"""

    if os.getenv("DEV_SOURCE_PATH", None) is None:
        # Only add CORS if we are in dev mode
        return {}

    data = {}
    if settings.get("GALAXY_ENABLE_CORS", default=False):
        corsmiddleware = ["galaxy_ng.app.common.openapi.AllowCorsMiddleware"]
        data["MIDDLEWARE"] = corsmiddleware + settings.get("MIDDLEWARE", [])
    return data


def configure_pulp_ansible(settings: Dynaconf) -> Dict[str, Any]:
    # Translate the galaxy default base path to the pulp ansible default base path.
    distro_path = settings.get("GALAXY_API_DEFAULT_DISTRIBUTION_BASE_PATH", "published")

    return {
        # ANSIBLE_URL_NAMESPACE tells pulp_ansible to generate hrefs and redirects that
        # point to the galaxy_ng api namespace. We're forcing it to get set to our api
        # namespace here because setting it to anything else will break our api.
        "ANSIBLE_URL_NAMESPACE": "galaxy:api:v3:",
        "ANSIBLE_DEFAULT_DISTRIBUTION_PATH": distro_path
    }


def configure_authentication_classes(settings: Dynaconf) -> Dict[str, Any]:
    # GALAXY_AUTHENTICATION_CLASSES is used to configure the galaxy api authentication
    # pretty much everywhere (on prem, cloud, dev environments, CI environments etc).
    # We need to set the REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES variable so that
    # the pulp APIs use the same authentication as the galaxy APIs. Rather than setting
    # the galaxy auth classes and the DRF classes in all those environments just set the
    # default rest framework auth classes to the galaxy auth classes. Ideally we should
    # switch everything to use the default DRF auth classes, but given how many
    # environments would have to be reconfigured, this is a lot easier.
    galaxy_auth_classes = settings.get("GALAXY_AUTHENTICATION_CLASSES", None)

    if galaxy_auth_classes:
        return {
            "REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES": galaxy_auth_classes
        }
    else:
        return {}


def configure_password_validators(settings: Dynaconf) -> Dict[str, Any]:
    """Configure the password validators"""
    GALAXY_MINIMUM_PASSWORD_LENGTH: int = settings.get("GALAXY_MINIMUM_PASSWORD_LENGTH", 9)
    AUTH_PASSWORD_VALIDATORS: List[Dict[str, Any]] = settings.AUTH_PASSWORD_VALIDATORS
    # NOTE: Dynaconf can't add or merge on dicts inside lists.
    # So we need to traverse the list to change it until the RFC is implemented
    # https://github.com/rochacbruno/dynaconf/issues/299#issuecomment-900616706
    for dict_item in AUTH_PASSWORD_VALIDATORS:
        if dict_item["NAME"].endswith("MinimumLengthValidator"):
            dict_item["OPTIONS"]["min_length"] = int(GALAXY_MINIMUM_PASSWORD_LENGTH)
    return {"AUTH_PASSWORD_VALIDATORS": AUTH_PASSWORD_VALIDATORS}


def configure_api_base_path(settings: Dynaconf) -> Dict[str, Any]:
    """Set the pulp api root under the galaxy api root."""

    galaxy_api_root = settings.get("GALAXY_API_PATH_PREFIX")
    pulp_api_root = f"/{galaxy_api_root.strip('/')}/pulp/"
    return {"API_ROOT": pulp_api_root}


def validate(settings: Dynaconf) -> None:
    """Validate the configuration, raise ValidationError if invalid"""
    settings.validators.register(
        Validator(
            "GALAXY_REQUIRE_SIGNATURE_FOR_APPROVAL",
            eq=False,
            when=Validator(
                "GALAXY_REQUIRE_CONTENT_APPROVAL", eq=False,
            ),
            messages={
                "operations": "{name} cannot be True if GALAXY_REQUIRE_CONTENT_APPROVAL is False"
            },
        ),
    )
    settings.validators.validate()
