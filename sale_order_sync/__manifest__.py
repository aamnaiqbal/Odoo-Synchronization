{
    "name": "Sale Order Sync",
    "version": "19.0.1.0.0",
    "summary": "One-way sync of new sale orders to a remote Odoo (queue + retry)",
    "category": "Sales",
    "license": "LGPL-3",
    "depends": ["sale", "server_connection"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/server_connection_views.xml",
        "views/sync_queue_views.xml",
    ],
    "installable": True,
    "application": False,
}
