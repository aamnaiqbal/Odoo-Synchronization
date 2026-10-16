{
    "name": "Odoo Synchronization",
    "version": "19.0.1.1.0",
    "category": "Technical",
    "summary": "Fetch configuration from a remote Odoo, then push local records to it",
    "depends": [
        "base",
        "web",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "views/sync_connection_views.xml",
        "views/sync_model_config_views.xml",
        "views/sync_run_views.xml",
        "views/sync_queue_views.xml",
        "views/sync_record_map_views.xml",
        "views/sync_registry_views.xml",
    ],
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
