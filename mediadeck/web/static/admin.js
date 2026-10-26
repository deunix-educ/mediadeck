// MediaDeck : page d'administration des lanceurs (/admin). Une classe par responsabilité.

import { $, I18n, Storage, STORAGE_TOKEN } from "./common.js";

// ----------------------------------------------------------------------
// Traductions fr/en
// ----------------------------------------------------------------------

const MESSAGES = {
    fr: {
        "admin.title": "Lanceurs",
        "admin.back": "← Télécommande",
        "admin.help": "Les lanceurs apparaissent dans l'onglet Lanceurs de la télécommande. Les modifications sont appliquées tout de suite.",
        "admin.empty": "Aucun lanceur pour l'instant.",
        "admin.add": "Ajouter un lanceur",
        "admin.edit": "Modifier",
        "admin.delete": "Supprimer",
        "admin.up": "Monter",
        "admin.down": "Descendre",
        "admin.confirm_delete": "Supprimer « {name} » ?",
        "admin.token_help": "Ouvrez d'abord la télécommande avec son URL complète (make url), puis revenez sur cette page.",
        "token.title": "Jeton d'accès manquant",
        "form.add_title": "Nouveau lanceur",
        "form.edit_title": "Modifier le lanceur",
        "form.label": "Nom",
        "form.icon": "Icône",
        "form.kind": "Type",
        "form.url": "Adresse (URL)",
        "form.browser": "Navigateur",
        "form.fullscreen": "Démarrer en plein écran",
        "form.fullscreen_unavailable": "Indisponible avec ce navigateur (le mode kiosque de Firefox ignore F11).",
        "form.not_installed": "{name} (non installé)",
        "form.app": "Application",
        "form.app_filter": "Filtrer…",
        "form.apps_empty": "Aucune application trouvée.",
        "form.app_missing": "{id} (introuvable)",
        "form.command": "Commande (un argument par ligne)",
        "form.command_help": "Pas de shell : chaque ligne est un argument, sans guillemets.",
        "form.command_locked": "Commandes brutes désactivées : seuls le nom et l'icône sont modifiables. Pour les autoriser, définir allow_custom_command = true dans la section [admin] de config.toml.",
        "form.save": "Enregistrer",
        "form.cancel": "Annuler",
        "kind.web": "Site web",
        "kind.app": "Application installée",
        "kind.command": "Commande (avancé)",
        "kind.default": "Application par défaut du système",
        "openers.title": "Lecteurs de fichiers multimédias",
        "openers.help": "Dans l'onglet Médias, un appui sur un fichier l'ouvre sur le PC avec le lecteur choisi ici pour son type. Le chemin du fichier est ajouté à la commande.",
        "openers.edit_title": "Lecteur : {type}",
        "openers.default_help": "Le fichier est ouvert avec l'application que le système associe à son format.",
        "openers.command_help": "Le chemin du fichier est ajouté en dernier argument. Pas de shell : chaque ligne est un argument, sans guillemets.",
        "openers.command_locked": "Commandes brutes désactivées : le lecteur ne peut être changé que par une application installée. Pour les autoriser, définir allow_custom_command = true dans la section [admin] de config.toml.",
        "openers.default_summary": "Application par défaut du système",
        "openers.tag_default": "Par défaut",
        "media.type.audio": "Son",
        "media.type.image": "Image",
        "media.type.video": "Vidéo",
        "icon.app": "Application",
        "icon.tv": "Télévision",
        "icon.film": "Film",
        "icon.globe": "Web",
        "icon.music": "Musique",
        "icon.screen-off": "Écran éteint",
        "icon.speaker": "Enceinte",
        "icon.lock": "Verrou",
        "icon.window": "Fenêtre",
        "error.auth": "Jeton refusé : rouvrez la télécommande avec son URL complète (make url).",
        "error.network": "Serveur injoignable.",
        "error.prefix": "Erreur : {message}",
    },
    en: {
        "admin.title": "Launchers",
        "admin.back": "← Remote",
        "admin.help": "Launchers appear in the Launchers tab of the remote. Changes apply immediately.",
        "admin.empty": "No launcher yet.",
        "admin.add": "Add a launcher",
        "admin.edit": "Edit",
        "admin.delete": "Delete",
        "admin.up": "Move up",
        "admin.down": "Move down",
        "admin.confirm_delete": "Delete “{name}”?",
        "admin.token_help": "Open the remote first with its full URL (make url), then come back to this page.",
        "token.title": "Missing access token",
        "form.add_title": "New launcher",
        "form.edit_title": "Edit launcher",
        "form.label": "Name",
        "form.icon": "Icon",
        "form.kind": "Type",
        "form.url": "Address (URL)",
        "form.browser": "Browser",
        "form.fullscreen": "Start fullscreen",
        "form.fullscreen_unavailable": "Unavailable with this browser (Firefox kiosk mode ignores F11).",
        "form.not_installed": "{name} (not installed)",
        "form.app": "Application",
        "form.app_filter": "Filter…",
        "form.apps_empty": "No application found.",
        "form.app_missing": "{id} (not found)",
        "form.command": "Command (one argument per line)",
        "form.command_help": "No shell: each line is one argument, without quotes.",
        "form.command_locked": "Raw commands are disabled: only the name and icon can be changed. To allow them, set allow_custom_command = true in the [admin] section of config.toml.",
        "form.save": "Save",
        "form.cancel": "Cancel",
        "kind.web": "Website",
        "kind.app": "Installed application",
        "kind.command": "Command (advanced)",
        "kind.default": "System default application",
        "openers.title": "Media file players",
        "openers.help": "In the Media tab, tapping a file opens it on the PC with the player chosen here for its type. The file path is added to the command.",
        "openers.edit_title": "Player: {type}",
        "openers.default_help": "The file is opened with the application the system associates with its format.",
        "openers.command_help": "The file path is added as the last argument. No shell: each line is one argument, without quotes.",
        "openers.command_locked": "Raw commands are disabled: the player can only be an installed application. To allow them, set allow_custom_command = true in the [admin] section of config.toml.",
        "openers.default_summary": "System default application",
        "openers.tag_default": "Default",
        "media.type.audio": "Sound",
        "media.type.image": "Image",
        "media.type.video": "Video",
        "icon.app": "Application",
        "icon.tv": "Television",
        "icon.film": "Film",
        "icon.globe": "Web",
        "icon.music": "Music",
        "icon.screen-off": "Screen off",
        "icon.speaker": "Speaker",
        "icon.lock": "Lock",
        "icon.window": "Window",
        "error.auth": "Token rejected: reopen the remote with its full URL (make url).",
        "error.network": "Server unreachable.",
        "error.prefix": "Error: {message}",
    },
};

// ----------------------------------------------------------------------
// Accès à l'API REST
// ----------------------------------------------------------------------

class ApiError extends Error {
    constructor(status, message) {
        super(message);
        this.status = status;
    }
}

class Api {
    constructor(token) {
        this.token = token;
    }

    async request(method, path, body) {
        const headers = { Authorization: `Bearer ${this.token}` };
        if (body !== undefined) {
            headers["Content-Type"] = "application/json";
        }
        let response;
        try {
            response = await fetch(path, {
                method,
                headers,
                body: body === undefined ? undefined : JSON.stringify(body),
                cache: "no-store",
            });
        } catch {
            throw new ApiError(0, "network");
        }
        if (response.status === 204) {
            return null;
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new ApiError(response.status, data.error ?? response.statusText);
        }
        return data;
    }

    list() {
        return this.request("GET", "/api/launchers");
    }

    // Liste mise en cache : les deux formulaires la partagent (réessayée si la requête échoue)
    apps() {
        this.appsRequest ??= this.request("GET", "/api/apps").catch((error) => {
            this.appsRequest = null;
            throw error;
        });
        return this.appsRequest;
    }

    openers() {
        return this.request("GET", "/api/media/openers");
    }

    setOpener(type, opener) {
        return this.request("PUT", `/api/media/openers/${encodeURIComponent(type)}`, opener);
    }

    create(entry) {
        return this.request("POST", "/api/launchers", entry);
    }

    update(id, entry) {
        return this.request("PUT", `/api/launchers/${encodeURIComponent(id)}`, entry);
    }

    remove(id) {
        return this.request("DELETE", `/api/launchers/${encodeURIComponent(id)}`);
    }

    reorder(ids) {
        return this.request("POST", "/api/launchers/reorder", { ids });
    }
}

// Message d'erreur affichable pour une exception d'API
function describeError(i18n, error) {
    if (error instanceof ApiError) {
        if (error.status === 401) {
            return i18n.t("error.auth");
        }
        if (error.status === 0) {
            return i18n.t("error.network");
        }
    }
    return i18n.t("error.prefix", { message: error.message });
}

function button(className, text, onClick) {
    const element = document.createElement("button");
    element.type = "button";
    element.className = className;
    element.textContent = text;
    element.addEventListener("click", onClick);
    return element;
}

// ----------------------------------------------------------------------
// Sélecteur d'application installée (liste filtrable), partagé par les deux formulaires
// ----------------------------------------------------------------------

class AppPicker {
    // ids : { select, filter, empty } ; onError(message) affiche une erreur dans le formulaire
    constructor(i18n, api, ids, onError) {
        this.i18n = i18n;
        this.api = api;
        this.onError = onError;
        this.select = $(ids.select);
        this.filter = $(ids.filter);
        this.empty = $(ids.empty);
        this.apps = null;
        this.selected = null;
        this.filter.addEventListener("input", () => this.render());
    }

    // Prépare une nouvelle ouverture du formulaire (application déjà choisie, filtre vidé)
    reset(selected) {
        this.selected = selected;
        this.filter.value = "";
        this.select.replaceChildren();
    }

    get value() {
        return this.select.value;
    }

    async load() {
        if (this.apps === null) {
            try {
                this.apps = await this.api.apps();
            } catch (error) {
                this.onError(describeError(this.i18n, error));
                return;
            }
        }
        this.render();
    }

    render() {
        if (this.apps === null) {
            return;
        }
        const current = this.select.value || this.selected;
        const filter = this.filter.value.trim().toLocaleLowerCase();
        const options = this.apps
            .filter((app) => app.name.toLocaleLowerCase().includes(filter) || app.id.toLocaleLowerCase().includes(filter))
            .map((app) => new Option(app.name, app.id));
        // Application enregistrée mais désinstallée depuis : on la montre au lieu de la perdre en silence
        if (this.selected && !this.apps.some((app) => app.id === this.selected)) {
            options.unshift(new Option(this.i18n.t("form.app_missing", { id: this.selected }), this.selected));
        }
        this.select.replaceChildren(...options);
        this.empty.hidden = options.length > 0;
        if (options.some((option) => option.value === current)) {
            this.select.value = current;
        }
    }
}

// ----------------------------------------------------------------------
// Liste des lanceurs
// ----------------------------------------------------------------------

class LauncherList {
    // handlers : { edit(entry), remove(entry), move(entry, delta) }
    constructor(i18n, handlers) {
        this.i18n = i18n;
        this.handlers = handlers;
    }

    render(launchers, browsers) {
        $("admin-empty").hidden = launchers.length > 0;
        $("admin-list").replaceChildren(
            ...launchers.map((entry, index) => this.item(entry, index, launchers.length, browsers)),
        );
    }

    static summary(entry, browsers) {
        switch (entry.kind) {
            case "web": {
                const browser = browsers.find((candidate) => candidate.id === entry.spec.browser);
                return `${entry.spec.url} · ${browser ? browser.name : entry.spec.browser}`;
            }
            case "app":
                return entry.spec.desktop_id;
            default:
                return entry.spec.argv.join(" ");
        }
    }

    item(entry, index, total, browsers) {
        const t = (key) => this.i18n.t(key);

        const title = document.createElement("div");
        title.className = "admin-item-title";
        const name = document.createElement("span");
        name.textContent = entry.label;
        const tag = document.createElement("span");
        tag.className = "kind-tag";
        tag.textContent = t(`kind.${entry.kind}`);
        title.append(name, tag);

        const summary = document.createElement("p");
        summary.className = "sub";
        summary.textContent = LauncherList.summary(entry, browsers);

        const text = document.createElement("div");
        text.className = "admin-item-text";
        text.append(title, summary);

        const actions = document.createElement("div");
        actions.className = "admin-actions";
        const up = button("chip", "↑", () => this.handlers.move(entry, -1));
        up.disabled = index === 0;
        up.setAttribute("aria-label", t("admin.up"));
        const down = button("chip", "↓", () => this.handlers.move(entry, 1));
        down.disabled = index === total - 1;
        down.setAttribute("aria-label", t("admin.down"));
        actions.append(
            up,
            down,
            button("chip", t("admin.edit"), () => this.handlers.edit(entry)),
            button("chip danger", t("admin.delete"), () => this.handlers.remove(entry)),
        );

        const item = document.createElement("li");
        item.className = "admin-item";
        item.append(text, actions);
        return item;
    }
}

// ----------------------------------------------------------------------
// Formulaire d'ajout / modification
// ----------------------------------------------------------------------

class LauncherForm {
    constructor(i18n, api, onSaved) {
        this.i18n = i18n;
        this.api = api;
        this.onSaved = onSaved;
        this.meta = null;
        this.editing = null;
        this.dialog = $("launcher-dialog");
        this.picker = new AppPicker(
            i18n,
            api,
            { select: "form-app", filter: "form-app-filter", empty: "form-apps-empty" },
            (message) => this.setError(message),
        );

        $("launcher-form").addEventListener("submit", (event) => {
            event.preventDefault();
            this.submit();
        });
        $("form-cancel").addEventListener("click", () => this.dialog.close());
        $("form-kind").addEventListener("change", () => this.showGroup());
        $("form-browser").addEventListener("change", () => this.updateFullscreen());
        i18n.onChange(() => {
            if (this.meta) {
                this.populate();
            }
        });
    }

    // (Re)construit les listes déroulantes, en conservant la sélection courante
    populate() {
        const t = (key, params) => this.i18n.t(key, params);
        const fill = (select, options) => {
            const previous = select.value;
            select.replaceChildren(
                ...options.map(({ value, text, disabled }) => {
                    const option = new Option(text, value);
                    option.disabled = Boolean(disabled);
                    return option;
                }),
            );
            if (options.some((option) => option.value === previous)) {
                select.value = previous;
            }
        };
        fill($("form-icon"), this.meta.icons.map((icon) => ({ value: icon, text: t(`icon.${icon}`) })));

        // Un lanceur « commande » existant reste modifiable (nom, icône) même si les commandes brutes sont interdites
        const commandAllowed = this.meta.allow_command || this.editing?.kind === "command";
        fill(
            $("form-kind"),
            ["web", "app", "command"].map((kind) => ({
                value: kind,
                text: t(`kind.${kind}`),
                disabled: kind === "command" && !commandAllowed,
            })),
        );

        const browserOptions = this.meta.browsers.map((browser) => ({
            value: browser.id,
            text: browser.installed ? browser.name : t("form.not_installed", { name: browser.name }),
            disabled: !browser.installed && browser.id !== this.editing?.spec?.browser,
        }));
        fill($("form-browser"), browserOptions);
    }

    open(meta, entry = null) {
        this.meta = meta;
        this.editing = entry;
        this.populate();
        $("form-title").textContent = this.i18n.t(entry ? "form.edit_title" : "form.add_title");
        this.setError("");

        const spec = entry?.spec ?? {};
        $("form-label").value = entry?.label ?? "";
        $("form-icon").value = entry?.icon ?? "tv";
        $("form-kind").value = entry?.kind ?? "web";
        $("form-url").value = spec.url ?? "";
        const firstInstalled = this.meta.browsers.find((browser) => browser.installed);
        $("form-browser").value = spec.browser ?? firstInstalled?.id ?? this.meta.browsers[0].id;
        $("form-fullscreen").checked = Boolean(spec.fullscreen);
        $("form-command").value = (spec.argv ?? []).join("\n");
        this.picker.reset(spec.desktop_id ?? null);

        const locked = entry?.kind === "command" && !this.meta.allow_command;
        $("form-command").disabled = locked;
        $("form-command-hint").dataset.i18n = locked ? "form.command_locked" : "form.command_help";
        $("form-command-hint").textContent = this.i18n.t(locked ? "form.command_locked" : "form.command_help");

        this.showGroup();
        this.updateFullscreen();
        this.dialog.showModal();
        $("form-label").focus();
    }

    showGroup() {
        const kind = $("form-kind").value;
        this.dialog.querySelectorAll("[data-group]").forEach((group) => {
            group.hidden = group.dataset.group !== kind;
        });
        if (kind === "app") {
            this.picker.load();
        }
    }

    updateFullscreen() {
        const browser = this.meta.browsers.find((candidate) => candidate.id === $("form-browser").value);
        const supported = Boolean(browser?.fullscreen);
        $("form-fullscreen").disabled = !supported;
        if (!supported) {
            $("form-fullscreen").checked = false;
        }
        $("form-fullscreen-hint").hidden = supported;
    }

    setError(message) {
        $("form-error").textContent = message;
        $("form-error").hidden = message === "";
    }

    // Arguments d'une commande : une ligne par argument, sans interprétation
    static parseArgv(text) {
        return text.split("\n").filter((line) => line !== "");
    }

    collect() {
        const kind = $("form-kind").value;
        let spec;
        if (kind === "web") {
            spec = {
                url: $("form-url").value.trim(),
                browser: $("form-browser").value,
                fullscreen: $("form-fullscreen").checked,
            };
        } else if (kind === "app") {
            spec = { desktop_id: this.picker.value };
        } else {
            spec = { argv: LauncherForm.parseArgv($("form-command").value) };
        }
        return { label: $("form-label").value.trim(), icon: $("form-icon").value, kind, spec };
    }

    async submit() {
        try {
            const entry = this.collect();
            if (this.editing) {
                await this.api.update(this.editing.id, entry);
            } else {
                await this.api.create(entry);
            }
        } catch (error) {
            this.setError(describeError(this.i18n, error));
            return;
        }
        this.dialog.close();
        this.onSaved();
    }
}

// ----------------------------------------------------------------------
// Lecteurs de fichiers multimédias (un par type : son, image, vidéo)
// ----------------------------------------------------------------------

class OpenerList {
    // onEdit(opener)
    constructor(i18n, onEdit) {
        this.i18n = i18n;
        this.onEdit = onEdit;
    }

    summary(opener) {
        switch (opener.kind) {
            case "app":
                return opener.spec.desktop_id;
            case "command":
                return opener.spec.argv.join(" ");
            default:
                return this.i18n.t("openers.default_summary");
        }
    }

    render(openers) {
        const t = (key) => this.i18n.t(key);
        $("opener-list").replaceChildren(
            ...openers.map((opener) => {
                const name = document.createElement("span");
                name.textContent = t(`media.type.${opener.type}`);
                const tag = document.createElement("span");
                tag.className = "kind-tag";
                tag.textContent = t(opener.kind === "default" ? "openers.tag_default" : `kind.${opener.kind}`);
                const title = document.createElement("div");
                title.className = "admin-item-title";
                title.append(name, tag);

                const summary = document.createElement("p");
                summary.className = "sub";
                summary.textContent = this.summary(opener);

                const text = document.createElement("div");
                text.className = "admin-item-text";
                text.append(title, summary);

                const actions = document.createElement("div");
                actions.className = "admin-actions";
                actions.append(button("chip", t("admin.edit"), () => this.onEdit(opener)));

                const item = document.createElement("li");
                item.className = "admin-item";
                item.append(text, actions);
                return item;
            }),
        );
    }
}

class OpenerForm {
    constructor(i18n, api, onSaved) {
        this.i18n = i18n;
        this.api = api;
        this.onSaved = onSaved;
        this.meta = null;
        this.opener = null;
        this.dialog = $("opener-dialog");
        this.picker = new AppPicker(
            i18n,
            api,
            { select: "opener-app", filter: "opener-app-filter", empty: "opener-apps-empty" },
            (message) => this.setError(message),
        );

        $("opener-form").addEventListener("submit", (event) => {
            event.preventDefault();
            this.submit();
        });
        $("opener-cancel").addEventListener("click", () => this.dialog.close());
        $("opener-kind").addEventListener("change", () => this.showGroup());
    }

    open(meta, opener) {
        this.meta = meta;
        this.opener = opener;
        const t = (key, params) => this.i18n.t(key, params);
        $("opener-title").textContent = t("openers.edit_title", { type: t(`media.type.${opener.type}`) });
        this.setError("");

        // Une commande déjà enregistrée reste visible et conservable même si les commandes brutes sont interdites
        const commandAllowed = meta.allow_command || opener.kind === "command";
        $("opener-kind").replaceChildren(
            ...meta.kinds.map((kind) => {
                const option = new Option(t(`kind.${kind}`), kind);
                option.disabled = kind === "command" && !commandAllowed;
                return option;
            }),
        );
        $("opener-kind").value = opener.kind;
        this.picker.reset(opener.spec.desktop_id ?? null);
        $("opener-command").value = (opener.spec.argv ?? []).join("\n");

        const locked = opener.kind === "command" && !meta.allow_command;
        $("opener-command").disabled = locked;
        $("opener-command-hint").textContent = t(locked ? "openers.command_locked" : "openers.command_help");

        this.showGroup();
        this.dialog.showModal();
    }

    showGroup() {
        const kind = $("opener-kind").value;
        this.dialog.querySelectorAll("[data-opener-group]").forEach((group) => {
            group.hidden = group.dataset.openerGroup !== kind;
        });
        if (kind === "app") {
            this.picker.load();
        }
    }

    setError(message) {
        $("opener-error").textContent = message;
        $("opener-error").hidden = message === "";
    }

    collect() {
        const kind = $("opener-kind").value;
        if (kind === "app") {
            return { kind, spec: { desktop_id: this.picker.value } };
        }
        if (kind === "command") {
            return { kind, spec: { argv: LauncherForm.parseArgv($("opener-command").value) } };
        }
        return { kind, spec: {} };
    }

    async submit() {
        try {
            await this.api.setOpener(this.opener.type, this.collect());
        } catch (error) {
            this.setError(describeError(this.i18n, error));
            return;
        }
        this.dialog.close();
        this.onSaved();
    }
}

// ----------------------------------------------------------------------
// Page
// ----------------------------------------------------------------------

class AdminPage {
    constructor() {
        this.i18n = new I18n(MESSAGES);
        this.i18n.apply();
        this.data = null;
        this.openers = null;

        const token = Storage.get(STORAGE_TOKEN);
        if (!token) {
            $("no-token").hidden = false;
            return;
        }

        this.api = new Api(token);
        this.list = new LauncherList(this.i18n, {
            edit: (entry) => this.form.open(this.data, entry),
            remove: (entry) => this.remove(entry),
            move: (entry, delta) => this.move(entry, delta),
        });
        this.form = new LauncherForm(this.i18n, this.api, () => this.refresh());
        this.openerList = new OpenerList(this.i18n, (opener) => this.openerForm.open(this.openers, opener));
        this.openerForm = new OpenerForm(this.i18n, this.api, () => this.refresh());

        $("admin-add").addEventListener("click", () => this.data && this.form.open(this.data));
        $("lang-toggle").addEventListener("click", () => this.i18n.toggle());
        this.i18n.onChange(() => {
            this.updateLangButton();
            this.render();
        });
        this.updateLangButton();
        this.refresh();
    }

    updateLangButton() {
        // Le bouton affiche la langue vers laquelle on bascule
        $("lang-toggle").textContent = this.i18n.lang === "fr" ? "EN" : "FR";
    }

    showError(error) {
        const message = error ? describeError(this.i18n, error) : "";
        $("admin-error").textContent = message;
        $("admin-error").hidden = message === "";
    }

    async refresh() {
        try {
            [this.data, this.openers] = await Promise.all([this.api.list(), this.api.openers()]);
        } catch (error) {
            this.showError(error);
            return;
        }
        this.showError(null);
        this.render();
    }

    render() {
        if (this.data) {
            this.list.render(this.data.launchers, this.data.browsers);
        }
        if (this.openers) {
            this.openerList.render(this.openers.openers);
        }
    }

    // Exécute une modification puis recharge la liste ; l'erreur éventuelle est affichée
    async apply(action) {
        try {
            await action();
        } catch (error) {
            this.showError(error);
            return;
        }
        await this.refresh();
    }

    move(entry, delta) {
        const ids = this.data.launchers.map((launcher) => launcher.id);
        const from = ids.indexOf(entry.id);
        const to = from + delta;
        if (from < 0 || to < 0 || to >= ids.length) {
            return Promise.resolve();
        }
        [ids[from], ids[to]] = [ids[to], ids[from]];
        return this.apply(() => this.api.reorder(ids));
    }

    remove(entry) {
        if (!window.confirm(this.i18n.t("admin.confirm_delete", { name: entry.label }))) {
            return Promise.resolve();
        }
        return this.apply(() => this.api.remove(entry.id));
    }
}

new AdminPage();
