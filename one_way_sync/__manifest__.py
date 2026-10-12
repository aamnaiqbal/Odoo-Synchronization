{
    "name": "One Way Odoo Synchronization",
    "version": "19.0.1.0.0",
    "category": "Technical",
    "summary": "One-way Local Odoo to Remote Odoo synchronization",
    "depends": [
        "base",
        "web",
    ],
    "data": [
        "security/ir.model.access.csv",

        "data/ir_cron.xml",

        "views/sync_connection_views.xml",
        "views/sync_queue_views.xml",
        "views/sync_registry_views.xml",
    ],
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}