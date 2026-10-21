import ast
import logging
from datetime import datetime, timedelta

import requests

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import SQL

_logger = logging.getLogger(__name__)

# Pages fetched per run. The cron may go through more data than a user waiting on a
# button; whatever is left is taken by the next run.
MAX_BATCHES_CRON = 50
MAX_BATCHES_MANUAL = 10

# Namespace of the advisory lock that keeps two pulls of one model from overlapping.
LOCK_NAMESPACE = 7312


def _parse_stamp(stamp):
    """'2026-10-21 10:00:00.123456' -> datetime (None if empty)."""
    return datetime.fromisoformat(stamp) if stamp else None


class SyncPullConfig(models.Model):
    """One model that is PULLED from the remote (remote -> local).

    The remote is the owner of these records (products, prices, ...): the local Odoo
    only receives them. Keep a model in ONE direction: a model that is pulled should
    not be pushed, and its records should not be edited locally (the next change made
    on the remote overwrites them).

    How a pull works
      * `cursor_stamp` / `cursor_id` is the (write_date, id) of the last record
        applied. Only records written after it are fetched, in that order. They are
        remote timestamps, so the clocks of the two servers do not matter.
      * A record created on the remote after the export has an id the local database
        may already use for something else. It is therefore stored under its own local
        id and linked to the remote one in sync.record.map. A record that came with
        the imported database (id <= baseline) keeps the same id on both sides.
      * Relations are translated back to local ids the same way. A relation to a model
        that is pulled too has to be pulled first (lower sequence), otherwise the record
        is not applied and the pull of this model stops with an error, so that no
        relation is ever lost silently.
      * Everything is written with `skip_sync`, so what is pulled is never pushed back.
    """

    _name = "sync.pull.config"
    _description = "Model Pulled From The Remote"
    _order = "sequence, id"
    _rec_name = "model_name"

    # Models offered by "Load Suggested Models", in pull order.
    SUGGESTED_MODELS = (
        ("product.category", 10, {}),
        ("product.template", 20, {"include_binary": True, "batch_size": 50}),
        ("product.pricelist", 30, {"track_deletes": True}),
        ("product.pricelist.item", 40, {"track_deletes": True}),
    )

    sequence = fields.Integer(
        default=10,
        help="Pull order. A model must come after the models it points to "
        "(categories before products, products before price list items).",
    )
    active = fields.Boolean(default=True)
    model_id = fields.Many2one(
        "ir.model",
        string="Model",
        required=True,
        ondelete="cascade",
        domain=[("transient", "=", False)],
    )
    model_name = fields.Char(string="Model Name", related="model_id.model", store=True, index=True)

    # ---- what to pull
    remote_domain = fields.Char(
        string="Remote Domain",
        default="[]",
        help="Only the remote records matching this domain are pulled. "
        "Example: [('sale_ok', '=', True)]",
    )
    excluded_fields = fields.Char(
        help="Technical names, separated by commas, that are never pulled.",
    )
    include_binary = fields.Boolean(
        string="Include Images / Files",
        help="Also pull binary fields (product images). Heavier: lower the batch size.",
    )
    batch_size = fields.Integer(default=200, help="Records fetched per request.")

    # ---- behaviour
    overlap_seconds = fields.Integer(
        default=300,
        help="Every run re-reads the changes of the last seconds before the cursor. "
        "A transaction that was still open on the remote when the previous run "
        "happened gets its records stamped with its START time, i.e. before the "
        "cursor; the overlap catches them. Records that are already up to date are skipped.",
    )
    track_deletes = fields.Boolean(
        string="Track Deletions",
        help="Archive (or delete, if the model cannot be archived) the local records "
        "whose remote record no longer exists. Needed for models whose records are "
        "deleted on the remote, such as price list items. Checked every few hours.",
    )
    reconcile_hours = fields.Integer(string="Check Deletions Every (hours)", default=6)

    # ---- progress
    cursor_stamp = fields.Char(
        readonly=True,
        copy=False,
        help="write_date (with microseconds) of the last remote record applied.",
    )
    cursor_id = fields.Integer(readonly=True, copy=False)
    cursor_ready = fields.Boolean(
        readonly=True,
        copy=False,
        help="The starting point of the first pull has been decided. Empty cursor + "
        "ready means: fetch everything from the beginning.",
    )
    last_pull_date = fields.Datetime(readonly=True, copy=False)
    last_pull_count = fields.Integer(readonly=True, copy=False)
    last_reconcile_date = fields.Datetime(readonly=True, copy=False)
    last_error = fields.Text(readonly=True, copy=False)
    failed_id = fields.Integer(readonly=True, copy=False)
    failed_stamp = fields.Char(readonly=True, copy=False)

    _model_uniq = models.Constraint(
        "UNIQUE(model_id)",
        "This model is already configured.",
    )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @api.constrains("model_id")
    def _check_model(self):
        for config in self:
            name = config.model_name
            if name.startswith("sync.") or name not in self.env:
                raise ValidationError(_("%s cannot be pulled.", name))
            Model = self.env[name]
            if Model._abstract or Model._transient or not Model._auto or "write_date" not in Model._fields:
                raise ValidationError(
                    _("%s cannot be pulled: it needs its own table with a write_date.", name)
                )

    @api.constrains("remote_domain")
    def _check_remote_domain(self):
        for config in self:
            config._get_remote_domain()

    def _get_remote_domain(self):
        self.ensure_one()
        try:
            domain = ast.literal_eval(self.remote_domain or "[]")
        except (ValueError, SyntaxError) as error:
            raise ValidationError(_("The remote domain is not valid: %s", error)) from error
        if not isinstance(domain, list):
            raise ValidationError(_("The remote domain must be a list."))
        return domain

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------
    @api.model
    def _cron_pull(self):
        connection = self.env["sync.connection"].sudo().search([("active", "=", True)], limit=1)
        if not connection or not connection.baseline_date:
            return
        self._pull_all(connection, MAX_BATCHES_CRON)

    def _pull_all(self, connection, max_batches=MAX_BATCHES_CRON):
        """Pull `self` (every active model when empty), one after the other in
        sequence order. Returns {"applied", "errors", "offline"}."""
        configs = self or self.search([("active", "=", True)])
        summary = {"applied": 0, "errors": 0, "offline": False}
        for config in configs:
            try:
                summary["applied"] += config._pull(connection, max_batches)
            except requests.RequestException as error:
                # Remote unreachable: normal for an offline-first setup, try again later.
                _logger.warning("[PULL] remote unreachable: %s", error)
                config.write({"last_error": _("Remote unreachable: %s", error)})
                summary["offline"] = True
                break
            except UserError as error:
                # The remote refused the request (access rights, unknown model, ...).
                _logger.warning("[PULL] %s: %s", config.model_name, error)
                config.write({"last_error": str(error)})
            if config.last_error:
                summary["errors"] += 1
        return summary

    @api.model
    def _load_suggested_models(self):
        """Create the pull configuration of the usual master data models that exist
        in this database. Returns the number created."""
        created = 0
        for model_name, sequence, extra in self.SUGGESTED_MODELS:
            if model_name not in self.env:
                continue
            model = self.env["ir.model"]._get(model_name)
            if self.with_context(active_test=False).search_count([("model_id", "=", model.id)]):
                continue
            self.create({"model_id": model.id, "sequence": sequence, **extra})
            created += 1
        return created

    # ------------------------------------------------------------------
    # Pull of one model
    # ------------------------------------------------------------------
    def _pull(self, connection, max_batches=MAX_BATCHES_CRON):
        """Fetch and apply the changes of this model. Returns the number of local
        records created or updated."""
        self.ensure_one()
        if not self.cursor_ready:
            self._init_cursor(connection)

        domain = self._get_remote_domain()
        excluded = [name.strip() for name in (self.excluded_fields or "").split(",") if name.strip()]
        since, since_id = self._get_request_cursor()

        total = 0
        caught_up = False
        for _page in range(max_batches):
            # Transaction level lock: released by the commit below, taken again per page.
            if not self._try_lock():
                return total

            result = connection._json2_call(
                "sync.registry",
                "fetch_changes",
                {
                    "model_name": self.model_name,
                    "since_write_date": since,
                    "since_id": since_id,
                    "domain": domain,
                    "limit": self.batch_size or 200,
                    "include_binary": self.include_binary,
                    "excluded_fields": excluded,
                },
            )
            rows = result.get("records") or []

            applied, failure = self._apply_rows(connection, rows)
            total += applied
            if failure:
                self.write({
                    "last_error": _("Remote record %(id)s: %(error)s", id=failure["id"], error=failure["error"]),
                    "failed_id": failure["id"],
                    "failed_stamp": failure["stamp"],
                    "last_pull_date": fields.Datetime.now(),
                    "last_pull_count": total,
                })
                self.env.cr.commit()
                return total

            if rows:
                since, since_id = rows[-1]["write_date"], rows[-1]["id"]
            self.env.cr.commit()

            if not result.get("has_more"):
                caught_up = True
                break

        # Deletions are only looked for once the model is up to date.
        if caught_up and self.track_deletes and self._reconcile_is_due():
            total += self._reconcile_deletes(connection)

        self.write({
            "last_error": False,
            "failed_id": 0,
            "failed_stamp": False,
            "last_pull_date": fields.Datetime.now(),
            "last_pull_count": total,
        })
        self.env.cr.commit()
        return total

    def _try_lock(self):
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, self.id))
        return self.env.cr.fetchone()[0]

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------
    def _init_cursor(self, connection):
        """Start from what the imported database already contains: the newest
        write_date among the records that came with the import. Without this the
        first pull would fetch the whole catalog again."""
        self.ensure_one()
        Model = self.env[self.model_name]
        baseline = connection._get_baseline_max_id(self.model_name)
        self.env.cr.execute(SQL(
            "SELECT write_date, id FROM %s WHERE id <= %s AND write_date IS NOT NULL "
            "ORDER BY write_date DESC, id DESC LIMIT 1",
            SQL.identifier(Model._table),
            baseline,
        ))
        row = self.env.cr.fetchone()
        self.write({
            "cursor_stamp": row[0].isoformat(sep=" ") if row else False,
            "cursor_id": row[1] if row else 0,
            "cursor_ready": True,
        })
        return bool(row)

    def _get_request_cursor(self):
        """Where the first page of a run starts: the cursor minus the overlap."""
        if not self.cursor_stamp:
            return False, 0
        start = _parse_stamp(self.cursor_stamp) - timedelta(seconds=self.overlap_seconds or 0)
        return start.isoformat(sep=" "), 0

    def _advance_cursor(self, stamp, record_id):
        """Move the cursor forward, never back (the overlap re-reads older records)."""
        new = (_parse_stamp(stamp), int(record_id))
        current = (_parse_stamp(self.cursor_stamp), self.cursor_id or 0) if self.cursor_stamp else None
        if current is None or new > current:
            self.write({"cursor_stamp": stamp, "cursor_id": int(record_id)})

    # ------------------------------------------------------------------
    # Applying the records
    # ------------------------------------------------------------------
    def _get_local_model(self):
        """The local model, set up so that what is written here is never queued for
        the push and leaves no mail / tracking noise."""
        return self.env[self.model_name].with_context(
            skip_sync=True,
            active_test=False,
            tracking_disable=True,
            mail_notrack=True,
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
        )

    def _apply_rows(self, connection, rows):
        """Apply a page, record by record. Stops at the first record that fails and
        leaves the cursor right before it: nothing is ever skipped silently.
        Returns (number applied, failure or False)."""
        applied = 0
        last_ok = None
        failure = False
        for row in rows:
            try:
                with self.env.cr.savepoint():
                    if self._apply_row(connection, row):
                        applied += 1
            except Exception as error:  # noqa: BLE001 - reported on the configuration
                _logger.exception("[PULL] %s remote id %s failed", self.model_name, row.get("id"))
                failure = {"id": row.get("id"), "stamp": row.get("write_date"), "error": str(error)}
                break
            last_ok = row
        if last_ok:
            self._advance_cursor(last_ok["write_date"], last_ok["id"])
        return applied, failure

    def _apply_row(self, connection, row):
        """Create or update the local record of one remote record.
        Returns False when it was already up to date."""
        Model = self._get_local_model()
        Map = self.env["sync.record.map"]
        remote_id = int(row["id"])
        stamp = row["write_date"]

        mapping = Map._get_mapping_by_remote(connection, self.model_name, remote_id)
        if mapping and mapping.remote_stamp == stamp and Model.browse(mapping.local_id).exists():
            return False

        # Which local record is it? Mapping row, same id (imported), or same xmlid.
        record = Model.browse()
        local_id = Map._get_local_id(connection, self.model_name, remote_id)
        if local_id:
            record = Model.browse(local_id)
        elif row.get("xmlid"):
            found = self.env.ref(row["xmlid"], raise_if_not_found=False)
            if found and found._name == self.model_name:
                record = Model.browse(found.id)

        warnings = []
        values = self._resolve_values(connection, Model, row.get("values") or {}, warnings)
        for warning in warnings:
            _logger.warning("[PULL] %s remote id %s: %s", self.model_name, remote_id, warning)

        if record:
            record.write(values)
        else:
            record = Model.create(values)

        Map._set(
            connection,
            self.model_name,
            record.id,
            remote_id,
            remote_write_date=fields.Datetime.to_datetime(stamp[:19]),
            remote_stamp=stamp,
        )
        self._pair_variants(connection, record, row)
        return True

    def _pair_variants(self, connection, record, row):
        """Variants are not configured here: a template has exactly one product.product.
        Link it to the remote one so that records that point to a variant (price list
        items, order lines pushed later) can be resolved in both directions."""
        if record._name != "product.template" or not row.get("variant_ids"):
            return
        remote_variants = row["variant_ids"]
        local_variants = record.with_context(active_test=False).product_variant_ids
        if len(remote_variants) == 1 and len(local_variants) == 1:
            self.env["sync.record.map"]._set(
                connection, "product.product", local_variants.id, int(remote_variants[0])
            )
        else:
            _logger.warning(
                "[PULL] product.template %s has several variants (remote %s, local %s): "
                "product variants are not supported, they are not paired.",
                record.id, len(remote_variants), len(local_variants),
            )

    # ------------------------------------------------------------------
    # Relations: remote keys -> local ids
    # ------------------------------------------------------------------
    def _resolve_values(self, connection, Model, values, warnings):
        result = {}
        for name, value in values.items():
            field = Model._fields.get(name)
            if field is None:
                warnings.append(f"{name}: no such field in this database, skipped")
                continue

            if field.type == "many2one":
                if not value:
                    result[name] = False
                    continue
                local_id = self._resolve_key(connection, value, name, warnings)
                if local_id:
                    result[name] = local_id

            elif field.type == "many2many":
                ids = []
                for key in value or []:
                    local_id = self._resolve_key(connection, key, name, warnings)
                    if local_id:
                        ids.append(local_id)
                result[name] = [Command.set(ids)]

            else:
                result[name] = value
        return result

    def _resolve_key(self, connection, key, field_name, warnings):
        """Local id of the record a relation key points to (False if it has none)."""
        model = key.get("model")
        if not model or model not in self.env:
            warnings.append(f"{field_name}: model {model} does not exist in this database, skipped")
            return False

        local_id = self.env["sync.record.map"]._get_local_id(connection, model, int(key["remote_id"]))
        if not local_id and key.get("xmlid"):
            found = self.env.ref(key["xmlid"], raise_if_not_found=False)
            if found and found._name == model:
                local_id = found.id
        if local_id:
            return local_id

        # A relation to a model that is pulled too must not be dropped: the record
        # is retried after that model has been pulled.
        if model in self.search([("active", "=", True)]).mapped("model_name"):
            raise UserError(
                _(
                    "%(field)s points to %(model)s %(id)s, which has not been pulled yet.",
                    field=field_name, model=model, id=key["remote_id"],
                )
            )
        warnings.append(f"{field_name}: {model} {key['remote_id']} does not exist locally, skipped")
        return False

    # ------------------------------------------------------------------
    # Deletions
    # ------------------------------------------------------------------
    def _reconcile_is_due(self):
        if not self.last_reconcile_date:
            return True
        due = self.last_reconcile_date + timedelta(hours=self.reconcile_hours or 6)
        return fields.Datetime.now() >= due

    def _reconcile_deletes(self, connection):
        """Archive / delete the local records whose remote record is gone.
        Returns the number of local records processed."""
        self.ensure_one()
        Model = self._get_local_model()
        Map = self.env["sync.record.map"]
        domain = self._get_remote_domain()

        remote_ids = set(connection._json2_call(
            "sync.registry", "fetch_ids", {"model_name": self.model_name, "domain": domain}
        ))

        maps = Map.search([("connection_id", "=", connection.id), ("model", "=", self.model_name)])
        local_ids = set(maps.filtered(lambda m: m.remote_id not in remote_ids).mapped("local_id"))

        # Records that came with the import and were never pulled have no mapping row.
        # With a domain they cannot be judged (outside the domain is not deleted), so
        # only an unrestricted pull looks at them.
        if not domain:
            baseline = connection._get_baseline_max_id(self.model_name)
            mapped_local = set(maps.mapped("local_id"))
            imported = Model.search([("id", "<=", baseline)]).ids
            local_ids |= {i for i in imported if i not in mapped_local and i not in remote_ids}

        # Safety net: an empty answer is more likely a wrong domain or missing
        # access rights than a remote that lost everything.
        if not remote_ids and local_ids:
            _logger.warning(
                "[PULL] %s: the remote returned no record at all, deletions not applied.",
                self.model_name,
            )
            return 0

        processed = 0
        for record in Model.browse(sorted(local_ids)).exists():
            try:
                with self.env.cr.savepoint():
                    if "active" in Model._fields:
                        if record.active:
                            record.write({"active": False})
                    else:
                        record.unlink()
                processed += 1
            except Exception:  # noqa: BLE001 - e.g. still referenced, kept as it is
                _logger.warning(
                    "[PULL] %s %s was deleted on the remote but could not be removed here.",
                    self.model_name, record.id,
                )

        # Forget the links of the records that no longer exist.
        maps.filtered(lambda m: not Model.browse(m.local_id).exists()).unlink()
        self.last_reconcile_date = fields.Datetime.now()
        return processed

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    @api.model
    def _notify(self, title, message, kind="success"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": kind,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def _pull_manually(self, connection):
        """Run a pull for a button: `self` (or every active model when empty)."""
        if not connection.baseline_date:
            raise UserError(_("Capture the baseline of the connection first."))
        summary = self._pull_all(connection, MAX_BATCHES_MANUAL)
        message = _("%s record(s) created or updated.", summary["applied"])
        kind = "success"
        if summary["errors"]:
            message += " " + _("%s model(s) have an error: see Pull Models.", summary["errors"])
            kind = "warning"
        if summary["offline"]:
            message += " " + _("The remote is unreachable.")
            kind = "warning"
        return self._notify(_("Pull finished"), message, kind)

    def _get_connection(self):
        connection = self.env["sync.connection"].sudo().search([("active", "=", True)], limit=1)
        if not connection:
            raise UserError(_("There is no active connection."))
        return connection

    def action_pull_now(self):
        self.ensure_one()
        return self._pull_manually(self._get_connection())

    def action_init_cursor(self):
        """Set the cursor from the local data again (the newest record that came with the import)."""
        self.ensure_one()
        found = self._init_cursor(self._get_connection())
        return self._notify(
            _("Cursor"),
            _("Cursor set from the local data.")
            if found
            else _("No imported record found: the next pull starts from the beginning."),
        )

    def action_reset_cursor(self):
        """Forget the progress: the next pull fetches every record again (a full resync)."""
        self.ensure_one()
        self.write({
            "cursor_stamp": False,
            "cursor_id": 0,
            "cursor_ready": True,
            "last_error": False,
            "failed_id": 0,
            "failed_stamp": False,
        })
        return self._notify(_("Cursor"), _("The next pull fetches every record again."), "warning")

    def action_skip_failed(self):
        """Give up on the record that blocks the pull: the cursor moves past it."""
        self.ensure_one()
        if not self.failed_stamp:
            raise UserError(_("There is no failed record."))
        self._advance_cursor(self.failed_stamp, self.failed_id)
        self.write({"last_error": False, "failed_id": 0, "failed_stamp": False})
        return self._notify(_("Skipped"), _("The failed record was skipped."))
