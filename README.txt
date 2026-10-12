Both container should be on the same network.
To check the DB in a container:
docker exec -it odoo-19copy-db-1 psql -U odoo -l
To test a connection from the source to the target: docker exec -it odoo-19-web-1 python3 -c "import requests; print(requests.get('http://odoo-19copy-web-1:8069/web/login', timeout=5).status_code)"
This is the source container name: odoo-19-web-1 and this is the target: odoo-19copy-web-1

docker ps --format "table {{.Names}}\t{{.Ports}}\t{{.Networks}}"

docker network create odoo-shared

docker network connect odoo-shared odoo-19-web-1


docker exec -it odoo-19-web-1 bash -lc "odoo -c /etc/odoo/odoo.conf -d test_1 -u one_way_sync --stop-after-init"