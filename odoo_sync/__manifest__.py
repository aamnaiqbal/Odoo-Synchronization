{
    "name": "Odoo Synchronization",
    "version": "19.0.2.0.0",
    "category": "Technical",
    "summary": "Synchronization between a local Odoo (a copy of the remote database) and a "
    "remote Odoo: local records are pushed, master data is pulled",
    "depends": [
        "base",
        "web",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "views/sync_connection_views.xml",
        "views/sync_pull_config_views.xml",
        "views/sync_queue_views.xml",
        "views/sync_record_map_views.xml",
        "views/sync_registry_views.xml",
    ],
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
