from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)
class SyncRegistry(models.Model):
    _name = "sync.registry"
    _description = "Synchronization Registry"
    _order = "id desc"

    name = fields.Char(
        required=True,
    )

    model_name = fields.Char(
        required=True,
        index=True,
    )

    sync_key = fields.Char(
        required=True,
        index=True,
    )

    remote_id = fields.Integer(
        required=True,
    )

    active = fields.Boolean(
        default=True,
    )

    _sql_constraints = [
        (
            "model_sync_key_unique",
            "unique(model_name, sync_key)",
            "A synchronization key must be unique per model.",
        ),
    ]

    @api.model
    def ping(self):
        return {
            "success": True,
            "message": "Sync server is reachable.",
        }

    @api.model
    def get_remote_id(self, model_name, sync_key):
        registry = self.search([
            ("model_name", "=", model_name),
            ("sync_key", "=", sync_key),
            ("active", "=", True),
        ], limit=1)

        return registry.remote_id if registry else False

    # @api.model
    # def apply(
    #     self,
    #     model_name,
    #     sync_uuid,
    #     operation,
    #     values=None,
    # ):
    #     if not model_name:
    #         raise UserError(_("Model name is required."))

    #     if not sync_uuid:
    #         raise UserError(_("Sync UUID is required."))

    #     Model = self.env[model_name]

    #     if not Model:
    #         raise UserError(
    #             _("Model %s does not exist.") % model_name
    #         )

    #     registry = self.search([
    #         ("model_name", "=", model_name),
    #         ("sync_key", "=", sync_uuid),
    #     ], limit=1)

    #     if operation == "delete":
    #         return self._apply_delete(
    #             registry,
    #             model_name,
    #             sync_uuid,
    #         )

    #     values = values or {}

    #     values = self._resolve_relations(values)

    #     if registry:
    #         record = Model.browse(registry.remote_id)

    #         if record.exists():
    #             record.with_context(
    #                 skip_sync=True
    #             ).write(values)

    #             return {
    #                 "success": True,
    #                 "remote_id": record.id,
    #                 "operation": "update",
    #             }

    #         registry.unlink()

    #     record = Model.with_context(
    #         skip_sync=True
    #     ).create(values)

    #     self.create({
    #         "name": f"{model_name}:{sync_uuid}",
    #         "model_name": model_name,
    #         "sync_key": sync_uuid,
    #         "remote_id": record.id,
    #     })

    #     return {
    #         "success": True,
    #         "remote_id": record.id,
    #         "operation": "create",
    #     }

    
    @api.model
    def apply(
        self,
        model_name,
        sync_uuid,
        operation,
        values=None,
    ):
        _logger.warning(
            "========== REMOTE SYNC APPLY START =========="
        )
        _logger.warning(
            "model_name=%s",
            model_name,
        )
        _logger.warning(
            "sync_uuid=%s",
            sync_uuid,
        )
        _logger.warning(
            "operation=%s",
            operation,
        )
        _logger.warning(
            "values=%s",
            values,
        )

        if not model_name:
            raise UserError(_("Model name is required."))

        if not sync_uuid:
            raise UserError(_("Sync UUID is required."))

        Model = self.env[model_name]

        _logger.warning(
            "REMOTE MODEL FOUND: %s",
            Model,
        )

        if model_name not in self.env:
            raise UserError(
                _("Model %s does not exist.") % model_name
            )

        Model = self.env[model_name]

        registry = self.search([
            ("model_name", "=", model_name),
            ("sync_key", "=", sync_uuid),
        ], limit=1)

        _logger.warning(
            "REMOTE REGISTRY: id=%s remote_id=%s",
            registry.id if registry else None,
            registry.remote_id if registry else None,
        )

        if operation == "delete":
            return self._apply_delete(
                registry,
                model_name,
                sync_uuid,
            )

        values = values or {}

        _logger.warning(
            "VALUES BEFORE RELATION RESOLUTION: %s",
            values,
        )

        values = self._resolve_relations(values)

        _logger.warning(
            "VALUES AFTER RELATION RESOLUTION: %s",
            values,
        )

        if registry:
            record = Model.browse(registry.remote_id)

            _logger.warning(
                "EXISTING REMOTE RECORD: exists=%s id=%s",
                record.exists(),
                record.id,
            )

            if record.exists():
                _logger.warning(
                    "UPDATING REMOTE RECORD id=%s",
                    record.id,
                )

                record.with_context(
                    skip_sync=True
                ).write(values)

                return {
                    "success": True,
                    "remote_id": record.id,
                    "operation": "update",
                }

            registry.unlink()

        _logger.warning(
            "CREATING REMOTE RECORD: model=%s values=%s",
            model_name,
            values,
        )

        record = Model.with_context(
            skip_sync=True
        ).create(values)

        _logger.warning(
            "REMOTE RECORD CREATED: id=%s",
            record.id,
        )

        self.create({
            "name": f"{model_name}:{sync_uuid}",
            "model_name": model_name,
            "sync_key": sync_uuid,
            "remote_id": record.id,
        })

        _logger.warning(
            "REMOTE REGISTRY CREATED: model=%s sync_uuid=%s remote_id=%s",
            model_name,
            sync_uuid,
            record.id,
        )

        return {
            "success": True,
            "remote_id": record.id,
            "operation": "create",
        }
    
    def _apply_delete(
        self,
        registry,
        model_name,
        sync_uuid,
    ):
        if not registry:
            return {
                "success": True,
                "remote_id": False,
                "operation": "delete",
            }

        record = self.env[model_name].browse(
            registry.remote_id
        )

        if record.exists():
            record.with_context(
                skip_sync=True
            ).unlink()

        registry.unlink()

        return {
            "success": True,
            "remote_id": False,
            "operation": "delete",
        }

    def _resolve_relations(self, values):
        _logger.warning(
            "========== RESOLVING RELATIONS =========="
        )

        result = {}

        for field_name, value in values.items():

            _logger.warning(
                "FIELD: %s | VALUE: %s",
                field_name,
                value,
            )

            if isinstance(value, dict):
                if (
                    "model" in value
                    and "sync_uuid" in value
                ):
                    _logger.warning(
                        "RELATION FOUND: field=%s model=%s sync_uuid=%s",
                        field_name,
                        value["model"],
                        value["sync_uuid"],
                    )

                    remote_id = self.get_remote_id(
                        value["model"],
                        value["sync_uuid"],
                    )

                    _logger.warning(
                        "RELATION REMOTE ID: %s",
                        remote_id,
                    )

                    if not remote_id:
                        raise UserError(
                            _(
                                "Related record not synchronized yet: "
                                "%s / %s"
                            )
                            % (
                                value["model"],
                                value["sync_uuid"],
                            )
                        )

                    result[field_name] = remote_id
                    continue

            if isinstance(value, list):
                resolved_list = []

                for item in value:
                    if (
                        isinstance(item, dict)
                        and "model" in item
                        and "sync_uuid" in item
                    ):
                        remote_id = self.get_remote_id(
                            item["model"],
                            item["sync_uuid"],
                        )

                        _logger.warning(
                            "M2M/O2M RELATION: field=%s model=%s uuid=%s remote_id=%s",
                            field_name,
                            item["model"],
                            item["sync_uuid"],
                            remote_id,
                        )

                        if not remote_id:
                            raise UserError(
                                _(
                                    "Related record not synchronized yet: "
                                    "%s / %s"
                                )
                                % (
                                    item["model"],
                                    item["sync_uuid"],
                                )
                            )

                        resolved_list.append(remote_id)

                    else:
                        resolved_list.append(item)

                result[field_name] = [
                    (6, 0, resolved_list)
                ]
                continue

            result[field_name] = value

        return result


    # def _resolve_relations(self, values):
    #     result = {}

    #     for field_name, value in values.items():

    #         if isinstance(value, dict):
    #             if (
    #                 "model" in value
    #                 and "sync_uuid" in value
    #             ):
    #                 remote_id = self.get_remote_id(
    #                     value["model"],
    #                     value["sync_uuid"],
    #                 )

    #                 if not remote_id:
    #                     raise UserError(
    #                         _(
    #                             "Related record not synchronized yet: "
    #                             "%s / %s"
    #                         )
    #                         % (
    #                             value["model"],
    #                             value["sync_uuid"],
    #                         )
    #                     )

    #                 result[field_name] = remote_id
    #                 continue

    #         if isinstance(value, list):
    #             resolved_list = []

    #             for item in value:
    #                 if (
    #                     isinstance(item, dict)
    #                     and "model" in item
    #                     and "sync_uuid" in item
    #                 ):
    #                     remote_id = self.get_remote_id(
    #                         item["model"],
    #                         item["sync_uuid"],
    #                     )

    #                     if not remote_id:
    #                         raise UserError(
    #                             _(
    #                                 "Related record not synchronized yet: "
    #                                 "%s / %s"
    #                             )
    #                             % (
    #                                 item["model"],
    #                                 item["sync_uuid"],
    #                             )
    #                         )

    #                     resolved_list.append(remote_id)

    #                 else:
    #                     resolved_list.append(item)

    #             result[field_name] = [
    #                 (6, 0, resolved_list)
    #             ]
    #             continue

    #         result[field_name] = value

    #     return result