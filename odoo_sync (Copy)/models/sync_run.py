import logging
from collections import Counter

from odoo import Command, _, api, fields, models

_logger = logging.getLogger(__name__)

# Context used for every write done by the importer. `sync_in_progress` is the flag
# your future push hooks must check, so imported records are not pushed back.
SYNC_CONTEXT = {
    "sync_in_progress": True,
    "skip_sync": True,
    "tracking_disable": True,
    "mail_create_nolog": True,
    "mail_notrack": True,
    "no_reset_password": True,
    "active_test": False,
}
COMMIT_EVERY = 200


class SyncRun(models.Model):
    _name = "sync.run"
    _description = "Configuration Fetch Run"
    _order = "id desc"

    connection_id = fields.Many2one(
        "sync.connection", required=True, ondelete="cascade", index=True
    )
    state = fields.Selection(
        [("queued", "Queued"), ("running", "Running"), ("done", "Done"), ("failed", "Failed")],
        default="queued",
        required=True,
        index=True,
    )
    push_after_fetch = fields.Boolean(
        readonly=True,
        help="When the fetch finishes without errors, queue every local record that "
        "does not exist on the remote yet and let the queue worker push them.",
    )
    date_start = fields.Datetime(readonly=True)
    date_end = fields.Datetime(readonly=True)
    line_ids = fields.One2many("sync.run.line", "run_id", string="Log")
    error_count = fields.Integer(compute="_compute_error_count")

    @api.depends("line_ids.level")
    def _compute_error_count(self):
        data = self.env["sync.run.line"]._read_group(
            [("run_id", "in", self.ids), ("level", "=", "error")], ["run_id"], ["__count"]
        )
        counts = {run.id: count for run, count in data}
        for run in self:
            run.error_count = counts.get(run.id, 0)

    @api.depends("connection_id")
    def _compute_display_name(self):
        for run in self:
            run.display_name = _("Run #%s", run.id)

    # ------------------------------------------------------------------
    # Cron entry point
    # ------------------------------------------------------------------
    @api.model
    def _cron_process_runs(self):
        for run in self.search([("state", "=", "queued")], order="id"):
            run._execute()

    # ------------------------------------------------------------------
    # Logging / commit helpers
    # ------------------------------------------------------------------
    def _log(self, level, message, model=False, remote_id=0):
        _logger.log(
            {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}[level],
            "[sync run %s] %s %s %s", self.id, model or "", remote_id or "", message,
        )
        self.env["sync.run.line"].create(
            {
                "run_id": self.id,
                "level": level,
                "model": model or False,
                "remote_id": remote_id or 0,
                "message": message,
            }
        )

    @staticmethod
    def _error_message(error):
        """Odoo's UserError/RedirectWarning keep the text in args[0]; str() would show a tuple."""
        return str(error.args[0]) if getattr(error, "args", None) else str(error)

    def _commit(self):
        self.env.flush_all()
        self.env.cr.commit()  # noqa: E501  (long import, keep progress if it dies later)

    # ------------------------------------------------------------------
    # Main flow
    # ------------------------------------------------------------------
    def _execute(self):
        self.ensure_one()
        self.write({"state": "running", "date_start": fields.Datetime.now()})
        self._commit()

        cache = {"resolved": {}, "xmlids": {}, "deferred": [], "unresolved": Counter()}
        state = "done"
        try:
            configs = self.env["sync.model.config"]._get_ordered_configs()
            cache["configured"] = set(configs.mapped("model_name"))
            lookup_configs = self.env["sync.model.config"].search([
                ("mode", "=", "lookup")
            ])

            cache["lookup"] = {
                c.model_name: c
                for c in lookup_configs
            }
            cache["lookup_miss"] = set()

                        # Logs 
            _logger.info(
                "[SYNC DEBUG] Run %s | Configured models: %s",
                self.id,
                sorted(cache["configured"]),
            )

            _logger.info(
                "[SYNC DEBUG] Run %s | Reference-only models: %s",
                self.id,
                {
                    config.model_name: config._get_match_fields()
                    for config in lookup_configs
                },
            )


            for config in configs:
                try:
                    self._sync_model(config, cache)
                except Exception as error:  # noqa: BLE001 - one model must not stop the run
                    self.env.cr.rollback()
                    state = "failed"
                    self._log(
                        "error", _("Model skipped: %s", self._error_message(error)), config.model_name
                    )
                    self._commit()
            self._resolve_deferred(cache)
            for (model, field, comodel), count in cache["unresolved"].items():
                self._log(
                    "warning",
                    _("%(model)s.%(field)s: %(count)s value(s) pointing to %(comodel)s could "
                      "not be resolved locally and were left empty.",
                      model=model, field=field, count=count, comodel=comodel),
                    model,
                )
        except Exception as error:  # noqa: BLE001
            self.env.cr.rollback()
            state = "failed"
            _logger.exception("Sync run %s aborted", self.id)
            self._log("error", _("Run aborted: %s", error))
        self.write({"state": state, "date_end": fields.Datetime.now()})
        if state == "done":
            self.connection_id.write(
                {"config_fetched": True, "last_fetch_date": fields.Datetime.now()}
            )
        self._commit()
        if self.push_after_fetch:
            self._push_local_records(state)

    def _push_local_records(self, state):
        """Phase 2: the remote ids are stored now, so local records can be pushed and
        their relations resolved."""
        self.ensure_one()
        if state != "done":
            self._log(
                "warning",
                _("Push skipped: the configuration fetch did not finish without errors."),
            )
            self._commit()
            return
        try:
            count = self.connection_id._enqueue_local_records()
            self._log("info", _("%s local record(s) queued for push.", count))
            self.env.ref("odoo_sync.ir_cron_sync_queue_worker").sudo()._trigger()
        except Exception as error:  # noqa: BLE001
            self.env.cr.rollback()
            _logger.exception("Sync run %s: queueing local records failed", self.id)
            self._log("error", _("Queueing local records failed: %s", self._error_message(error)))
        self._commit()

    def _sync_model(self, config, cache):
        connection = self.connection_id
        model_name = config.model_name
        field_names = config._get_fetch_fields(connection)
        domain = config._get_domain()
        stats = Counter()
        after_id = 0
        while True:
            result = connection._json2_call(
                "sync.registry",
                "fetch_batch",
                {
                    "model_name": model_name,
                    "field_names": field_names,
                    "domain": domain,
                    "after_id": after_id,
                    "limit": connection.batch_size,
                },
            )
            rows = result["records"]
            if not rows:
                break
            self._import_rows(config, field_names, rows, cache, stats)
            after_id = rows[-1]["id"]
            self._commit()
            self.env.invalidate_all()
            if len(rows) < connection.batch_size:
                break
        self._log(
            "info",
            _("created %(c)s, updated %(u)s, skipped %(s)s, failed %(f)s",
              c=stats["created"], u=stats["updated"], s=stats["skipped"], f=stats["failed"]),
            model_name,
        )
        self._commit()

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    def _import_rows(self, config, field_names, rows, cache, stats):
        connection = self.connection_id
        model_name = config.model_name
        Model = self.env[model_name].with_context(**SYNC_CONTEXT)
        Map = self.env["sync.record.map"]
        maps = Map._load_by_remote(connection, model_name, [r["id"] for r in rows])

        for row in rows:
            remote_id = row["id"]
            remote_wd = fields.Datetime.to_datetime(row.get("write_date")) or False
            mapping = maps.get(remote_id)
            local = Model.browse(mapping.local_id).exists() if mapping else Model.browse()

            # already synced and nothing to do
            if local and (not config.update_existing or mapping.remote_write_date == remote_wd):
                stats["skipped"] += 1
                continue

            try:
                with self.env.cr.savepoint():
                    vals, deferred = self._prepare_vals(Model, field_names, row, model_name, cache)
                    if not local:
                        local = self._find_existing(config, Model, row, vals)
                        if local:
                            # Matched a pre-existing local record (usually standard data
                            # such as the admin/public partners): keep its archived state.
                            vals.pop("active", None)
                    if local:
                        if config.update_existing:
                            local.write(vals)
                            stats["updated"] += 1
                        else:
                            stats["skipped"] += 1
                    else:
                        local = Model.create(vals)
                        stats["created"] += 1
                    Map._set(
                        connection, model_name, local.id, remote_id,
                        xmlid=row.get("_sync_xmlid"), remote_write_date=remote_wd,
                    )
            except Exception as error:  # noqa: BLE001 - log and continue with next record
                stats["failed"] += 1
                self._log("error", self._error_message(error), model_name, remote_id)
                continue

            cache["resolved"][(model_name, remote_id)] = local.id
            for item in deferred:
                item.update(model=model_name, local_id=local.id)
                cache["deferred"].append(item)

    def _prepare_vals(self, Model, field_names, row, model_name, cache):
        """Translate remote values to local ones. Relational values that cannot be
        resolved yet (target created later in the run) are returned as `deferred`."""
        vals, deferred = {}, []
        for name in field_names:
            if name not in row:
                continue
            field = Model._fields[name]
            value = row[name]
            if field.type == "many2one":
                if not value:
                    vals[name] = False
                    continue
                local_id = self._resolve(field.comodel_name, value, cache)
                if local_id:
                    vals[name] = local_id
                else:
                    self._defer_or_count(
                        deferred, cache, model_name, name, field.comodel_name, [value]
                    )
            elif field.type == "many2many":
                resolved, missing = [], []
                for remote_id in value or []:
                    local_id = self._resolve(field.comodel_name, remote_id, cache)
                    (resolved if local_id else missing).append(local_id or remote_id)
                vals[name] = [Command.set(resolved)]
                if missing:
                    self._defer_or_count(
                        deferred, cache, model_name, name, field.comodel_name, missing
                    )
            else:
                vals[name] = value
        return vals, deferred

    def _defer_or_count(self, deferred, cache, model_name, field_name, comodel, remote_ids):
        if comodel in cache["configured"]:
            deferred.append(
                {"field": field_name, "comodel": comodel, "remote_ids": remote_ids}
            )
        else:
            cache["unresolved"][(model_name, field_name, comodel)] += len(remote_ids)

    def _find_existing(self, config, Model, row, vals):
        """Look for a local record that corresponds to the remote one (first sync)."""
        xmlid = row.get("_sync_xmlid")
        if xmlid:
            record = self.env.ref(xmlid, raise_if_not_found=False)
            if record and record._name == config.model_name:
                return record.with_context(**SYNC_CONTEXT)
        domain = []
        for name in config._get_match_fields():
            value = vals.get(name)
            if value in (False, None, ""):
                return Model.browse()
            domain.append((name, "=", value))
        return Model.search(domain, limit=1) if domain else Model.browse()

    # ------------------------------------------------------------------
    # Remote id -> local id
    # ------------------------------------------------------------------
    # def _resolve(self, comodel, remote_id, cache):
    #     key = (comodel, remote_id)
    #     if key in cache["resolved"]:
    #         return cache["resolved"][key]
    #     connection = self.connection_id
    #     local_id = self.env["sync.record.map"]._get_local_id(connection, comodel, remote_id)
    #     if not local_id:
    #         xmlid = self._remote_xmlid(comodel, remote_id, cache)
    #         if xmlid:
    #             record = self.env.ref(xmlid, raise_if_not_found=False)
    #             if record and record._name == comodel:
    #                 local_id = record.id
    #     if not local_id and comodel in cache["lookup"] and key not in cache["lookup_miss"]:
    #         local_id = self._lookup_local(cache["lookup"][comodel], remote_id)
    #         if not local_id:
    #             cache["lookup_miss"].add(key)
    #     if local_id:
    #         cache["resolved"][key] = local_id
    #     return local_id


    def _resolve(self, comodel, remote_id, cache):
        """Resolve a remote record to its corresponding local record."""

        key = (comodel, remote_id)

        _logger.info(
            "[SYNC DEBUG] Resolving relation | model=%s | remote_id=%s",
            comodel,
            remote_id,
        )

        # ---------------------------------------------------------
        # Step 1: Check the in-memory cache
        # ---------------------------------------------------------
        if key in cache["resolved"]:
            local_id = cache["resolved"][key]

            _logger.info(
                "[SYNC DEBUG] Resolution found in cache | "
                "model=%s | remote_id=%s | local_id=%s",
                comodel,
                remote_id,
                local_id,
            )

            return local_id

        connection = self.connection_id

        # ---------------------------------------------------------
        # Step 2: Check sync.record.map
        # ---------------------------------------------------------
        local_id = self.env["sync.record.map"]._get_local_id(
            connection,
            comodel,
            remote_id,
        )

        if local_id:
            _logger.info(
                "[SYNC DEBUG] Resolution found in sync.record.map | "
                "model=%s | remote_id=%s | local_id=%s",
                comodel,
                remote_id,
                local_id,
            )

        # ---------------------------------------------------------
        # Step 3: Check the remote XML ID
        # ---------------------------------------------------------
        if not local_id:
            xmlid = self._remote_xmlid(comodel, remote_id, cache)

            _logger.info(
                "[SYNC DEBUG] XML ID lookup | "
                "model=%s | remote_id=%s | xmlid=%s",
                comodel,
                remote_id,
                xmlid or False,
            )

            if xmlid:
                record = self.env.ref(
                    xmlid,
                    raise_if_not_found=False,
                )

                if record and record._name == comodel:
                    local_id = record.id

                    _logger.info(
                        "[SYNC DEBUG] Resolution found by XML ID | "
                        "model=%s | remote_id=%s | local_id=%s",
                        comodel,
                        remote_id,
                        local_id,
                    )

        # ---------------------------------------------------------
        # Step 4: Reference only lookup
        # ---------------------------------------------------------
        if not local_id and comodel in cache["lookup"]:

            if key in cache["lookup_miss"]:
                _logger.warning(
                    "[SYNC DEBUG] Reference lookup previously failed | "
                    "model=%s | remote_id=%s",
                    comodel,
                    remote_id,
                )

            else:
                config = cache["lookup"][comodel]

                _logger.info(
                    "[SYNC DEBUG] Starting Reference only lookup | "
                    "model=%s | remote_id=%s | match_fields=%s",
                    comodel,
                    remote_id,
                    config._get_match_fields(),
                )

                local_id = self._lookup_local(config, remote_id)

                if local_id:
                    _logger.info(
                        "[SYNC DEBUG] Reference only lookup succeeded | "
                        "model=%s | remote_id=%s | local_id=%s",
                        comodel,
                        remote_id,
                        local_id,
                    )
                else:
                    _logger.warning(
                        "[SYNC DEBUG] Reference only lookup failed | "
                        "model=%s | remote_id=%s",
                        comodel,
                        remote_id,
                    )

                    cache["lookup_miss"].add(key)

        elif not local_id:
            _logger.warning(
                "[SYNC DEBUG] No Reference only configuration found | "
                "model=%s | remote_id=%s",
                comodel,
                remote_id,
            )

        # ---------------------------------------------------------
        # Step 5: Cache successful resolution
        # ---------------------------------------------------------
        if local_id:
            cache["resolved"][key] = local_id

        else:
            _logger.warning(
                "[SYNC DEBUG] Could not resolve remote record | "
                "model=%s | remote_id=%s",
                comodel,
                remote_id,
            )

        return local_id

    # def _lookup_local(self, config, remote_id):
    #     """'Reference only' models: read the one remote record, find the local record
    #     with the same match-field values, and map them."""
    #     Model = self.env[config.model_name].with_context(active_test=False)
    #     names = [
    #         n for n in config._get_match_fields()
    #         if Model._fields[n].type not in ("many2one", "many2many", "one2many")
    #     ]
    #     if not names:
    #         return False
    #     try:
    #         result = self.connection_id._json2_call(
    #             "sync.registry",
    #             "fetch_batch",
    #             {
    #                 "model_name": config.model_name,
    #                 "field_names": names,
    #                 "domain": [("id", "=", remote_id)],
    #                 "after_id": 0,
    #                 "limit": 1,
    #             },
    #         )
    #     except Exception:  # noqa: BLE001 - no access / model missing remotely
    #         return False
    #     rows = result.get("records") or []
    #     if not rows:
    #         return False
    #     row = rows[0]
    #     domain = []
    #     for name in names:
    #         value = row.get(name)
    #         if value in (False, None, ""):
    #             return False
    #         domain.append((name, "=", value))
    #     record = Model.search(domain, limit=1)
    #     if not record:
    #         return False
    #     self.env["sync.record.map"]._set(
    #         self.connection_id, config.model_name, record.id, remote_id,
    #         xmlid=row.get("_sync_xmlid"),
    #         remote_write_date=fields.Datetime.to_datetime(row.get("write_date")) or False,
    #     )
    #     return record.id

    def _lookup_local(self, config, remote_id):
        """Reference-only lookup: find a local record using remote match-field values."""

        model_name = config.model_name

        # Include all local companies in the search context.
        # This prevents company-dependent searches from receiving an empty
        # allowed_company_ids list.
        company_ids = self.env["res.company"].sudo().search([]).ids

        if not company_ids:
            _logger.error(
                "[SYNC DEBUG] Reference lookup aborted: "
                "no local companies found | model=%s | remote_id=%s",
                model_name,
                remote_id,
            )
            return False

        Model = (
            self.env[model_name]
            .sudo()
            .with_context(
                active_test=False,
                allowed_company_ids=company_ids,
            )
        )

        names = [
            name
            for name in config._get_match_fields()
            if Model._fields[name].type
            not in ("many2one", "many2many", "one2many")
        ]

        if not names:
            _logger.warning(
                "[SYNC DEBUG] No usable match fields | model=%s",
                model_name,
            )
            return False

        # Fetch the remote record's matching values.
        try:
            result = self.connection_id._json2_call(
                "sync.registry",
                "fetch_batch",
                {
                    "model_name": model_name,
                    "field_names": names,
                    "domain": [("id", "=", remote_id)],
                    "after_id": 0,
                    "limit": 1,
                },
            )
        except Exception:
            _logger.exception(
                "[SYNC DEBUG] Failed to fetch remote record | "
                "model=%s | remote_id=%s",
                model_name,
                remote_id,
            )
            return False

        rows = result.get("records") or []

        if not rows:
            _logger.warning(
                "[SYNC DEBUG] Remote record not found | "
                "model=%s | remote_id=%s",
                model_name,
                remote_id,
            )
            return False

        row = rows[0]
        domain = []

        for name in names:
            value = row.get(name)

            if value in (False, None, ""):
                _logger.warning(
                    "[SYNC DEBUG] Empty matching value | "
                    "model=%s | remote_id=%s | field=%s",
                    model_name,
                    remote_id,
                    name,
                )
                return False

            domain.append((name, "=", value))

        _logger.info(
            "[SYNC DEBUG] Searching local reference | "
            "model=%s | remote_id=%s | domain=%s | companies=%s",
            model_name,
            remote_id,
            domain,
            company_ids,
        )

        try:
            record = Model.search(domain, limit=1)
        except Exception:
            _logger.exception(
                "[SYNC DEBUG] Local reference search failed | "
                "model=%s | remote_id=%s | domain=%s",
                model_name,
                remote_id,
                domain,
            )
            return False

        if not record:
            _logger.warning(
                "[SYNC DEBUG] No matching local record | "
                "model=%s | remote_id=%s | domain=%s",
                model_name,
                remote_id,
                domain,
            )
            return False

        # Save the remote-to-local mapping.
        self.env["sync.record.map"]._set(
            self.connection_id,
            model_name,
            record.id,
            remote_id,
            xmlid=row.get("_sync_xmlid"),
            remote_write_date=(
                fields.Datetime.to_datetime(row.get("write_date")) or False
            ),
        )

        _logger.info(
            "[SYNC DEBUG] Reference lookup successful | "
            "model=%s | remote_id=%s | local_id=%s",
            model_name,
            remote_id,
            record.id,
        )

        return record.id


    def _remote_xmlid(self, comodel, remote_id, cache):
        key = (comodel, remote_id)
        if key not in cache["xmlids"]:
            try:
                result = self.connection_id._json2_call(
                    "sync.registry", "get_xmlids", {"model_name": comodel, "record_ids": [remote_id]}
                )
                cache["xmlids"][key] = result.get(str(remote_id)) or False
            except Exception:  # noqa: BLE001 - e.g. no access to that model remotely
                cache["xmlids"][key] = False
        return cache["xmlids"][key]

    def _resolve_deferred(self, cache):
        """Second pass: every model has been imported, now fill the pending relations."""
        for index, item in enumerate(cache["deferred"], 1):
            Model = self.env[item["model"]].with_context(**SYNC_CONTEXT)
            record = Model.browse(item["local_id"]).exists()
            if not record:
                continue
            resolved, missing = [], 0
            for remote_id in item["remote_ids"]:
                local_id = self._resolve(item["comodel"], remote_id, cache)
                if local_id:
                    resolved.append(local_id)
                else:
                    missing += 1
            try:
                with self.env.cr.savepoint():
                    field = Model._fields[item["field"]]
                    if resolved and field.type == "many2one":
                        record.write({item["field"]: resolved[0]})
                    elif resolved:
                        record.write({item["field"]: [Command.link(i) for i in resolved]})
            except Exception as error:  # noqa: BLE001
                self._log("error", _("Second pass failed: %s", error), item["model"])
            if missing:
                cache["unresolved"][(item["model"], item["field"], item["comodel"])] += missing
            if index % COMMIT_EVERY == 0:
                self._commit()
        self._commit()


class SyncRunLine(models.Model):
    _name = "sync.run.line"
    _description = "Configuration Fetch Run Log"
    _order = "id"

    run_id = fields.Many2one("sync.run", required=True, ondelete="cascade", index=True)
    level = fields.Selection(
        [("info", "Info"), ("warning", "Warning"), ("error", "Error")],
        default="info",
        required=True,
    )
    model = fields.Char()
    remote_id = fields.Integer()
    message = fields.Text()