PYTHON ?= .venv/bin/python
PIP ?= .venv/bin/pip
ANSIBLE_PLAYBOOK ?= .venv/bin/ansible-playbook
INVENTORY ?= inventory.ini
STOP_LEGACY ?= 1
RELEASE ?=

.PHONY: bootstrap validate render compose-config stage apply dry-run smoke legacy-check legacy-stop ufw-apply rollback rebalance clean-generated

bootstrap:
	python3 -m venv .venv
	$(PIP) install -r requirements.txt

validate:
	$(PYTHON) scripts/validate_models.py

render: validate
	$(PYTHON) scripts/render.py

compose-config: render
	DGX_ENV_FILE=.env.example docker compose --project-directory . --env-file .env.example -f generated/docker-compose.generated.yml config

stage: render
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/apply.yml -e stop_legacy=0 -e pull_images=0 -e compose_up=0

apply: render
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/apply.yml -e stop_legacy=$(STOP_LEGACY)

dry-run: render
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/apply.yml --check -e stop_legacy=0

smoke:
	$(PYTHON) scripts/smoke_test.py --host spark.cg-rookies.net --ssh-user ymin

legacy-check:
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/legacy-check.yml

legacy-stop:
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/legacy-stop.yml

ufw-apply:
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/ufw-wireguard.yml

rollback:
	@if [ -z "$(RELEASE)" ]; then echo "usage: make rollback RELEASE=<release-directory>"; exit 2; fi
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/rollback.yml -e release=$(RELEASE)

rebalance:
	$(PYTHON) scripts/rebalance.py --write
	$(PYTHON) scripts/validate_models.py

clean-generated:
	rm -f generated/docker-compose.generated.yml generated/litellm.config.yaml generated/prometheus-vllm-targets.yml generated/traefik-dynamic.yml
