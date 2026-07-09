PY := ./.venv/bin/python

.PHONY: install bootstrap sync-now dry-run reprocess install-agent logs

install:           ## create venv + install deps
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install requests phonenumbers pytypedstream

bootstrap:         ## create the Attio Text Threads object (idempotent)
	$(PY) -m scripts.bootstrap_attio_object

sync-now:          ## one sync pass
	$(PY) -m src.main --once

dry-run:           ## one pass, no Attio writes, prints the plan
	$(PY) -m src.main --once --dry-run

reprocess:         ## re-examine all history (does not advance watermark)
	$(PY) -m src.main --once --reprocess

install-agent:     ## install + load the launchd agent
	bash scripts/install_launchd.sh

logs:              ## tail the sync log
	tail -f ~/.imessage-attio/sync.log
