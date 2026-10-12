import requests
import logging 

from odoo import fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SyncConnection(models.Model):
    _name = "sync.connection"
    _description = "Remote Odoo Synchronization Connection"

    name = fields.Char(
        required=True,
    )

    active = fields.Boolean(
        default=True,
    )

    base_url = fields.Char(
        string="Remote URL",
        required=True,
        help="Example: https://my-odoo.example.com",
    )

    database = fields.Char(
        required=True,
    )

    api_key = fields.Char(
        required=True,
        copy=False,
    )

    timeout = fields.Integer(
        default=30,
        required=True,
    )

    batch_size = fields.Integer(
        default=50,
        required=True,
    )

    def _get_headers(self):
        self.ensure_one()

        return {
            "Authorization": f"bearer {self.api_key}",
            "X-Odoo-Database": self.database,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # def _json2_call(self, model, method, payload):
    #     self.ensure_one()

    #     url = (
    #         self.base_url.rstrip("/")
    #         + "/json/2/"
    #         + model
    #         + "/"
    #         + method
    #     )

    #     response = requests.post(
    #         url,
    #         headers=self._get_headers(),
    #         json=payload,
    #         timeout=self.timeout,
    #     )

    #     response.raise_for_status()

    #     return response.json()


    def _json2_call(self, model, method, payload):
        self.ensure_one()

        url = (
            self.base_url.rstrip("/")
            + "/json/2/"
            + model
            + "/"
            + method
        )

        _logger.warning(
            "========== JSON-2 REQUEST =========="
        )
        _logger.warning(
            "URL: %s",
            url,
        )
        _logger.warning(
            "MODEL: %s",
            model,
        )
        _logger.warning(
            "METHOD: %s",
            method,
        )
        _logger.warning(
            "PAYLOAD: %s",
            payload,
        )

        response = requests.post(
            url,
            headers=self._get_headers(),
            json=payload,
            timeout=self.timeout,
        )

        _logger.warning(
            "========== JSON-2 RESPONSE =========="
        )
        _logger.warning(
            "STATUS CODE: %s",
            response.status_code,
        )
        _logger.warning(
            "RESPONSE BODY: %s",
            response.text[:5000],
        )

        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            _logger.exception(
                "JSON-2 HTTP ERROR: %s",
                error,
            )

            raise UserError(
                _(
                    "Remote JSON-2 request failed.\n\n"
                    "Status: %s\n"
                    "URL: %s\n\n"
                    "Remote response:\n%s"
                )
                % (
                    response.status_code,
                    url,
                    response.text[:3000],
                )
            ) from error

        return response.json()

    def action_test_connection(self):
        self.ensure_one()

        try:
            result = self._json2_call(
                "sync.registry",
                "ping",
                {},
            )
        except requests.RequestException as error:
            raise UserError(
                _("Connection failed:\n%s", error)
            ) from error

        raise UserError(
            _("Connection successful.\n\nRemote response:\n%s", result)
        )