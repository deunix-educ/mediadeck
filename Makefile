# MediaDeck : opérations d'installation et d'exploitation (Debian / LMDE)
# Note : les recettes make exigent des tabulations (exception à la règle des 4 espaces).

SHELL := /bin/bash
PROJECT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
VENV := $(PROJECT_DIR)/.venv
PYTHON := $(VENV)/bin/python
CONFIG_DIR := $(or $(XDG_CONFIG_HOME),$(HOME)/.config)/mediadeck
CONFIG := $(CONFIG_DIR)/config.toml
UNIT_DIR := $(or $(XDG_CONFIG_HOME),$(HOME)/.config)/systemd/user
UNIT := $(UNIT_DIR)/mediadeck.service
UDEV_RULE := /etc/udev/rules.d/60-mediadeck-uinput.rules
PORT ?= 8765
# Port HTTP en clair facultatif (server.http_port), à ouvrir aussi : make firewall HTTP_PORT=8766
HTTP_PORT ?=

.DEFAULT_GOAL := help
.PHONY: help deps install config token url qr cert http-on http-off check run dev lint test uinput uinput-remove \
	service-install service-uninstall status logs firewall clean distclean

help: ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[33m%-18s\033[0m %s\n", $$1, $$2}'

deps: ## Installe les paquets système (sudo)
	sudo apt-get update
	sudo apt-get install -y python3-venv python3-dev gcc linux-libc-dev \
		pulseaudio-utils qrencode libglib2.0-bin libxkbcommon0 xkb-data

$(PYTHON):
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install --upgrade pip

install: $(PYTHON) ## Crée le venv et installe le projet (+ outils de dev)
	$(PYTHON) -m pip install -e '.[dev]'

config: ## Crée ~/.config/mediadeck/config.toml avec un jeton aléatoire (sans écraser)
	@mkdir -p $(CONFIG_DIR) && chmod 700 $(CONFIG_DIR)
	@if [ -e $(CONFIG) ]; then \
		echo "$(CONFIG) existe déjà (make token pour changer le jeton)"; \
	else \
		umask 077 && cp config.example.toml $(CONFIG) && $(MAKE) --no-print-directory token; \
		echo "Configuration créée : $(CONFIG)"; \
	fi

token: ## Régénère le jeton d'accès (les tablettes devront rescanner le QR)
	@umask 077 && python3 -c 'import re, secrets, pathlib; \
		p = pathlib.Path("$(CONFIG)"); \
		t = p.read_text(); \
		n = re.sub(r"(?m)^token\s*=.*$$", "token = \"" + secrets.token_urlsafe(32) + "\"", t, count=1); \
		p.write_text(n); p.chmod(0o600)'
	@echo "Nouveau jeton écrit dans $(CONFIG)"

url: $(PYTHON) ## Affiche l'URL tablette et son QR code
	@$(PYTHON) -m mediadeck --url

qr: $(PYTHON) ## Ouvre dans le navigateur du PC une page avec les QR codes à scanner
	@$(PYTHON) -m mediadeck --qr

cert: $(PYTHON) ## Crée la CA locale + le certificat HTTPS et active HTTPS (openssl)
	@$(PYTHON) -m mediadeck --make-cert

http-on: $(PYTHON) ## Ajoute un port HTTP en clair (non sécurisé) à côté du HTTPS (HTTP_PORT=8766)
	@$(PYTHON) -m mediadeck --http-port $(or $(HTTP_PORT),8766)

http-off: $(PYTHON) ## Retire le port HTTP en clair : HTTPS seul
	@$(PYTHON) -m mediadeck --http-port 0

check: $(PYTHON) ## Valide la configuration
	$(PYTHON) -m mediadeck --check

run: $(PYTHON) ## Lance le serveur au premier plan
	$(PYTHON) -m mediadeck

dev: $(PYTHON) ## Lance le serveur en mode verbeux
	$(PYTHON) -m mediadeck -v

lint: $(PYTHON) ## Vérification de types (pyright)
	$(VENV)/bin/pyright

test: $(PYTHON) ## Tests unitaires
	$(PYTHON) -m unittest discover -s tests -v

uinput: ## Autorise /dev/uinput pour la session active (sudo, règle udev uaccess)
	sudo install -m 644 udev/60-mediadeck-uinput.rules $(UDEV_RULE)
	sudo modprobe uinput
	echo uinput | sudo tee /etc/modules-load.d/mediadeck-uinput.conf > /dev/null
	sudo udevadm control --reload-rules
	sudo udevadm trigger --name-match=uinput
	@getfacl /dev/uinput 2>/dev/null | grep -q "user:$(USER)" && echo "OK : accès uinput accordé" \
		|| echo "Vérifier : getfacl /dev/uinput (une reconnexion de session peut être nécessaire)"

uinput-remove: ## Retire la règle udev uinput (sudo)
	sudo rm -f $(UDEV_RULE) /etc/modules-load.d/mediadeck-uinput.conf
	sudo udevadm control --reload-rules

service-install: install ## Installe et démarre le service utilisateur systemd
	@mkdir -p $(UNIT_DIR)
	sed 's|@PROJECT_DIR@|$(PROJECT_DIR)|g' systemd/mediadeck.service.in > $(UNIT)
	systemctl --user daemon-reload
	systemctl --user enable --now mediadeck.service
	@systemctl --user --no-pager status mediadeck.service | head -n 5

service-uninstall: ## Arrête et supprime le service utilisateur
	-systemctl --user disable --now mediadeck.service
	rm -f $(UNIT)
	systemctl --user daemon-reload

status: ## État du service
	systemctl --user --no-pager status mediadeck.service

logs: ## Journal du service (suivi)
	journalctl --user -u mediadeck.service -f

firewall: ## Ouvre le port aux réseaux privés si ufw est actif (sudo)
	@if command -v ufw > /dev/null; then \
		for net in 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12; do \
			for port in $(PORT) $(HTTP_PORT); do \
				sudo ufw allow from $$net to any port $$port proto tcp comment mediadeck; \
			done; \
		done; \
	else \
		echo "ufw absent : ouvrir le port $(PORT) $(HTTP_PORT) (tcp) pour le LAN uniquement"; \
	fi

clean: ## Supprime les caches
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf build dist *.egg-info .pyright

distclean: clean ## Supprime aussi le venv
	rm -rf $(VENV)
