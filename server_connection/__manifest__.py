{
    "name": "Server Connection",
    "version": "19.0.1.0.0",
    "summary": "Store and test the connection to a remote (cloud) Odoo server",
    "category": "Technical",
    "license": "LGPL-3",
    "depends": ["base"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.model.access.csv",
        "views/server_connection_views.xml",
    ],
    "installable": True,
    "application": False,
}
