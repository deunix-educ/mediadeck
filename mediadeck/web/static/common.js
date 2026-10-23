// MediaDeck : éléments partagés entre l'interface tablette (app.js) et la page d'administration (admin.js).

export const STORAGE_TOKEN = "mediadeck.token";
export const STORAGE_LANG = "mediadeck.lang";

export class Storage {
    // localStorage peut être indisponible (navigation privée, kiosque)
    static get(key) {
        try {
            return window.localStorage.getItem(key);
        } catch {
            return null;
        }
    }

    static set(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch {
            // Sans persistance : l'URL avec jeton reste nécessaire à chaque ouverture
        }
    }
}

export class I18n {
    // messages : { fr: {clé: texte}, en: {…} } ; la langue choisie est partagée entre les pages
    constructor(messages) {
        this.messages = messages;
        const stored = Storage.get(STORAGE_LANG);
        const browser = (navigator.language || "fr").slice(0, 2);
        this.lang = stored in messages ? stored : browser in messages ? browser : "en";
        this.listeners = [];
    }

    t(key, params = {}) {
        const template = this.messages[this.lang][key] ?? this.messages.en[key] ?? key;
        return template.replace(/\{(\w+)\}/g, (_, name) => params[name] ?? "");
    }

    // Applique les traductions aux éléments marqués data-i18n / data-i18n-label / data-i18n-placeholder
    apply(root = document) {
        document.documentElement.lang = this.lang;
        root.querySelectorAll("[data-i18n]").forEach((el) => {
            el.textContent = this.t(el.dataset.i18n);
        });
        root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
            el.setAttribute("placeholder", this.t(el.dataset.i18nPlaceholder));
        });
        root.querySelectorAll("[data-i18n-label]").forEach((el) => {
            const label = this.t(el.dataset.i18nLabel);
            el.setAttribute("aria-label", label);
            el.setAttribute("title", label);
        });
    }

    toggle() {
        this.lang = this.lang === "fr" ? "en" : "fr";
        Storage.set(STORAGE_LANG, this.lang);
        this.apply();
        this.listeners.forEach((listener) => listener(this.lang));
    }

    onChange(listener) {
        this.listeners.push(listener);
    }
}

export function $(id) {
    return document.getElementById(id);
}
