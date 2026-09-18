PYTHON ?= .venv/bin/python
PIP ?= .venv/bin/pip
ANSIBLE_PLAYBOOK ?= .venv/bin/ansible-playbook
INVENTORY ?= inventory.ini
PULL_IMAGES ?= 1
SERVICES_PULL_IMAGES ?= 1
RELEASE ?=

.PHONY: bootstrap validate render compose-config services-compose-config migrate-services stage apply dry-run smoke ufw-apply rollback rebalance clean-generated

bootstrap:
	python3 -m venv .venv
	$(PIP) install -r requirements.txt

validate:
	$(PYTHON) scripts/validate_models.py

render: validate
	$(PYTHON) scripts/render.py

compose-config: render
	DGX_ENV_FILE=.env.example docker compose --project-directory . --env-file .env.example -f generated/docker-compose.generated.yml config

services-compose-config:
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/migrate-services.yml -e pull_images=0 -e compose_up=0

migrate-services:
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/migrate-services.yml -e pull_images=$(SERVICES_PULL_IMAGES)

stage: render
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/apply.yml -e pull_images=0 -e compose_up=0

apply: render
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/apply.yml -e pull_images=$(PULL_IMAGES)

dry-run: render
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/apply.yml --check

smoke:
	$(PYTHON) scripts/smoke_test.py --host pika.ihopper.co.kr --ssh-user ymin

ufw-apply:
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/ufw-wireguard.yml

rollback:
	@if [ -z "$(RELEASE)" ]; then echo "usage: make rollback RELEASE=<release-directory>"; exit 2; fi
	$(ANSIBLE_PLAYBOOK) -i $(INVENTORY) playbooks/rollback.yml -e release=$(RELEASE)

rebalance:
	$(PYTHON) scripts/rebalance.py --write
	$(PYTHON) scripts/validate_models.py

clean-generated:
	rm -rf generated/docker-compose.generated.yml generated/litellm.config.yaml generated/prometheus-vllm-targets.yml generated/vllm-launch.generated.yml generated/vllm-launch
