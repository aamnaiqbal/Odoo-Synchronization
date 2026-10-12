{
    "name": "Partner Sync",
    "version": "19.0.1.0.0",
    "summary": "When a contact is created, create it on the remote Odoo too",
    "category": "Technical",
    "license": "LGPL-3",
    "depends": ["base", "server_connection"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "data/ir_cron.xml",
        "views/server_connection_views.xml",
        "views/contact_views.xml",
    ],
    "installable": True,
    "application": False,
}
