// MediaDeck : interface tablette. Une classe par responsabilité.

import { $, I18n, Storage, STORAGE_TOKEN } from "./common.js";

const STORAGE_TAB = "mediadeck.tab";

// ----------------------------------------------------------------------
// Traductions fr/en
// ----------------------------------------------------------------------

const MESSAGES = {
    fr: {
        "status.connecting": "Connexion…",
        "status.online": "Connecté",
        "status.offline": "Hors ligne",
        "status.auth": "Jeton refusé ?",
        "action.fullscreen": "Plein écran",
        "tab.player": "Lecture",
        "tab.audio": "Son",
        "tab.remote": "Pavé",
        "tab.launchers": "Lanceurs",
        "tab.system": "Système",
        "player.none": "Aucun lecteur multimédia actif",
        "player.unknown": "Titre inconnu",
        "player.seek": "Position",
        "player.shuffle": "Aléatoire",
        "player.previous": "Précédent",
        "player.play_pause": "Lecture / pause",
        "player.next": "Suivant",
        "player.loop": "Répétition",
        "player.volume": "Volume du lecteur",
        "player.raise": "Afficher la fenêtre",
        "audio.none": "Contrôle audio indisponible (pactl absent)",
        "audio.master": "Volume principal",
        "audio.mute": "Couper le son",
        "audio.outputs": "Sorties audio",
        "audio.default": "Par défaut",
        "remote.none": "Clavier et souris virtuels indisponibles",
        "remote.hint": "Glisser : déplacer · Toucher : clic · 2 doigts : défiler / clic droit",
        "text.open": "Clavier",
        "text.title": "Saisie de texte",
        "text.placeholder": "Texte à saisir sur le PC",
        "text.hint": "Tapé dans la fenêtre active du PC, comme sur son clavier.",
        "text.mask": "Masquer le texte",
        "text.send": "Envoyer",
        "text.send_enter": "Envoyer ↵",
        "text.close": "Fermer",
        "text.sent": "Texte envoyé",
        "text.unavailable": "Saisie de texte indisponible : {reason}",
        "key.up": "Haut",
        "key.down": "Bas",
        "key.left": "Gauche",
        "key.right": "Droite",
        "key.space": "Espace",
        "key.meta": "Super",
        "key.back": "Effacer",
        "key.page_up": "Pg ↑",
        "key.page_down": "Pg ↓",
        "launchers.title": "Applications et sites",
        "launchers.manage": "Gérer",
        "launchers.empty": "Aucun lanceur. Ajoutez-en depuis « Gérer ».",
        "launchers.launched": "Lancé : {name}",
        "system.session": "Session",
        "system.lock": "Verrouiller",
        "system.suspend": "Veille",
        "system.reboot": "Redémarrer",
        "system.poweroff": "Éteindre",
        "system.hold_hint": "Maintenir appuyé 1 seconde pour veille, redémarrage et extinction.",
        "system.power_unavailable": "Actions d'alimentation indisponibles",
        "token.title": "Jeton d'accès manquant",
        "token.help": "Ouvrez l'URL complète affichée sur le PC (ou scannez son QR code) :",
        "error.prefix": "Erreur : {message}",
    },
    en: {
        "status.connecting": "Connecting…",
        "status.online": "Connected",
        "status.offline": "Offline",
        "status.auth": "Token rejected?",
        "action.fullscreen": "Fullscreen",
        "tab.player": "Playing",
        "tab.audio": "Sound",
        "tab.remote": "Touchpad",
        "tab.launchers": "Launchers",
        "tab.system": "System",
        "player.none": "No active media player",
        "player.unknown": "Unknown title",
        "player.seek": "Position",
        "player.shuffle": "Shuffle",
        "player.previous": "Previous",
        "player.play_pause": "Play / pause",
        "player.next": "Next",
        "player.loop": "Repeat",
        "player.volume": "Player volume",
        "player.raise": "Show window",
        "audio.none": "Audio control unavailable (pactl missing)",
        "audio.master": "Master volume",
        "audio.mute": "Mute",
        "audio.outputs": "Audio outputs",
        "audio.default": "Default",
        "remote.none": "Virtual keyboard and mouse unavailable",
        "remote.hint": "Drag: move · Tap: click · 2 fingers: scroll / right click",
        "text.open": "Keyboard",
        "text.title": "Text entry",
        "text.placeholder": "Text to type on the PC",
        "text.hint": "Typed into the PC's active window, as on its keyboard.",
        "text.mask": "Hide text",
        "text.send": "Send",
        "text.send_enter": "Send ↵",
        "text.close": "Close",
        "text.sent": "Text sent",
        "text.unavailable": "Text entry unavailable: {reason}",
        "key.up": "Up",
        "key.down": "Down",
        "key.left": "Left",
        "key.right": "Right",
        "key.space": "Space",
        "key.meta": "Super",
        "key.back": "Backspace",
        "key.page_up": "PgUp",
        "key.page_down": "PgDn",
        "launchers.title": "Apps and sites",
        "launchers.manage": "Manage",
        "launchers.empty": "No launcher yet. Add some from “Manage”.",
        "launchers.launched": "Launched: {name}",
        "system.session": "Session",
        "system.lock": "Lock",
        "system.suspend": "Sleep",
        "system.reboot": "Restart",
        "system.poweroff": "Shut down",
        "system.hold_hint": "Hold for 1 second to sleep, restart or shut down.",
        "system.power_unavailable": "Power actions unavailable",
        "token.title": "Missing access token",
        "token.help": "Open the full URL shown on the PC (or scan its QR code):",
        "error.prefix": "Error: {message}",
    },
};

// ----------------------------------------------------------------------
// Utilitaires
// ----------------------------------------------------------------------

class Throttle {
    // Limite la fréquence d'envoi tout en garantissant l'envoi de la dernière valeur
    constructor(delayMs, callback) {
        this.delayMs = delayMs;
        this.callback = callback;
        this.timer = null;
        this.pending = undefined;
        this.last = 0;
    }

    push(value) {
        this.pending = value;
        const wait = this.delayMs - (performance.now() - this.last);
        if (wait <= 0) {
            this.flush();
        } else if (this.timer === null) {
            this.timer = setTimeout(() => this.flush(), wait);
        }
    }

    flush() {
        if (this.timer !== null) {
            clearTimeout(this.timer);
            this.timer = null;
        }
        if (this.pending === undefined) {
            return;
        }
        const value = this.pending;
        this.pending = undefined;
        this.last = performance.now();
        this.callback(value);
    }
}

function formatTime(microseconds) {
    const total = Math.max(0, Math.floor(microseconds / 1e6));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = String(total % 60).padStart(2, "0");
    return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
}

// Met à jour la couleur de remplissage d'un input range via une variable CSS
function paintRange(input) {
    const min = Number(input.min) || 0;
    const max = Number(input.max) || 100;
    const ratio = max > min ? (Number(input.value) - min) / (max - min) : 0;
    input.style.setProperty("--fill", `${(ratio * 100).toFixed(2)}%`);
}

// ----------------------------------------------------------------------
// Connexion WebSocket avec reconnexion
// ----------------------------------------------------------------------

class Connection extends EventTarget {
    constructor(token) {
        super();
        this.token = token;
        this.socket = null;
        this.retryDelay = 500;
        this.failuresBeforeOpen = 0;
        this.everOpened = false;
    }

    connect() {
        const scheme = location.protocol === "https:" ? "wss" : "ws";
        const url = `${scheme}://${location.host}/ws?token=${encodeURIComponent(this.token)}`;
        this.emit("status", "connecting");
        this.socket = new WebSocket(url);
        this.socket.addEventListener("open", () => {
            this.retryDelay = 500;
            this.failuresBeforeOpen = 0;
            this.everOpened = true;
            this.emit("status", "online");
        });
        this.socket.addEventListener("message", (event) => {
            try {
                const message = JSON.parse(event.data);
                this.emit(message.type, message);
            } catch (error) {
                console.error("invalid message", error);
            }
        });
        this.socket.addEventListener("close", () => this.scheduleReconnect());
    }

    scheduleReconnect() {
        this.socket = null;
        // Échecs répétés sans jamais ouvrir : probablement un jeton invalide (HTTP 401)
        this.failuresBeforeOpen += 1;
        const suspectAuth = !this.everOpened && this.failuresBeforeOpen >= 3;
        this.emit("status", suspectAuth ? "auth" : "offline");
        setTimeout(() => this.connect(), this.retryDelay);
        this.retryDelay = Math.min(this.retryDelay * 2, 8000);
    }

    send(message) {
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this.socket.send(JSON.stringify(message));
        }
    }

    emit(type, detail) {
        this.dispatchEvent(new CustomEvent(type, { detail }));
    }
}

// ----------------------------------------------------------------------
// Panneau lecture (MPRIS)
// ----------------------------------------------------------------------

class PlayerPanel {
    constructor(connection, i18n) {
        this.connection = connection;
        this.i18n = i18n;
        this.players = [];
        this.selected = null;
        this.userSelected = false;
        this.receivedAt = 0;
        this.seeking = false;

        this.els = {
            empty: $("player-empty"),
            view: $("player-view"),
            tabs: $("player-tabs"),
            coverImg: $("cover-img"),
            title: $("track-title"),
            artist: $("track-artist"),
            album: $("track-album"),
            seek: $("seek"),
            pos: $("time-pos"),
            len: $("time-len"),
            playIcon: $("play-icon"),
            shuffle: $("btn-shuffle"),
            loop: $("btn-loop"),
            loopBadge: $("loop-badge"),
            volumeRow: $("player-volume-row"),
            volume: $("player-volume"),
            raise: $("btn-raise"),
        };

        this.volumeThrottle = new Throttle(80, (value) => this.command("set_volume", value));
        this.bindEvents();
        // Interpolation locale de la position entre deux états serveur
        setInterval(() => this.renderPosition(), 250);
    }

    get current() {
        return this.players.find((player) => player.name === this.selected) ?? null;
    }

    command(action, value) {
        const player = this.current;
        if (player) {
            this.connection.send({ domain: "player", action, player: player.name, value });
        }
    }

    bindEvents() {
        $("btn-play").addEventListener("click", () => this.command("play_pause"));
        $("btn-next").addEventListener("click", () => this.command("next"));
        $("btn-prev").addEventListener("click", () => this.command("previous"));
        this.els.shuffle.addEventListener("click", () => {
            this.command("shuffle", !this.current?.shuffle);
        });
        this.els.loop.addEventListener("click", () => {
            const order = ["None", "Playlist", "Track"];
            const index = order.indexOf(this.current?.loop ?? "None");
            this.command("loop", order[(index + 1) % order.length]);
        });
        this.els.raise.addEventListener("click", () => this.command("raise"));

        const seek = this.els.seek;
        seek.addEventListener("pointerdown", () => {
            this.seeking = true;
        });
        seek.addEventListener("input", () => {
            this.seeking = true;
            const length = this.current?.metadata.length ?? 0;
            this.els.pos.textContent = formatTime((Number(seek.value) / 1000) * length);
            paintRange(seek);
        });
        seek.addEventListener("change", () => {
            const player = this.current;
            if (player && player.metadata.length > 0) {
                // Seek relatif : fonctionne même sans trackid valide
                const target = (Number(seek.value) / 1000) * player.metadata.length;
                this.command("seek", Math.round(target - this.position(player)));
            }
            this.seeking = false;
        });

        this.els.volume.addEventListener("input", () => {
            paintRange(this.els.volume);
            this.volumeThrottle.push(Number(this.els.volume.value) / 100);
        });
    }

    update(mpris) {
        this.players = mpris.players ?? [];
        this.receivedAt = performance.now();
        const exists = this.players.some((player) => player.name === this.selected);
        // Suit automatiquement le lecteur actif tant que l'utilisateur n'a rien choisi
        if (!exists || !this.userSelected) {
            this.selected = mpris.active;
            this.userSelected = this.userSelected && exists;
        }
        this.render();
    }

    position(player) {
        let position = player.position ?? 0;
        if (player.status === "Playing") {
            position += (performance.now() - this.receivedAt) * 1000;
        }
        const length = player.metadata.length ?? 0;
        return length > 0 ? Math.min(position, length) : position;
    }

    render() {
        const player = this.current;
        this.els.empty.hidden = player !== null;
        this.els.view.hidden = player === null;
        this.renderTabs();
        if (!player) {
            return;
        }

        const meta = player.metadata;
        this.els.title.textContent = meta.title || this.i18n.t("player.unknown");
        this.els.artist.textContent = meta.artist || "";
        this.els.album.textContent = meta.album || "";
        this.renderCover(meta.art);

        const playing = player.status === "Playing";
        this.els.playIcon.setAttribute("href", playing ? "#i-pause" : "#i-play");
        $("btn-next").disabled = !player.can_next;
        $("btn-prev").disabled = !player.can_previous;
        this.els.seek.disabled = !player.can_seek || !(meta.length > 0);

        this.els.shuffle.hidden = player.shuffle === null || player.shuffle === undefined;
        this.els.shuffle.classList.toggle("on", Boolean(player.shuffle));
        this.els.loop.hidden = !player.loop;
        this.els.loop.classList.toggle("on", Boolean(player.loop) && player.loop !== "None");
        this.els.loopBadge.hidden = player.loop !== "Track";

        const hasVolume = typeof player.volume === "number";
        this.els.volumeRow.hidden = !hasVolume;
        if (hasVolume && document.activeElement !== this.els.volume) {
            this.els.volume.value = String(Math.round(player.volume * 100));
            paintRange(this.els.volume);
        }
        this.els.raise.hidden = !player.can_raise;
        this.renderPosition();
    }

    renderTabs() {
        const tabs = this.els.tabs;
        tabs.hidden = this.players.length < 2;
        tabs.replaceChildren(
            ...this.players.map((player) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "chip" + (player.name === this.selected ? " active" : "");
                button.textContent = player.identity;
                if (player.status === "Playing") {
                    button.classList.add("live");
                }
                button.addEventListener("click", () => {
                    this.selected = player.name;
                    this.userSelected = true;
                    this.render();
                });
                return button;
            }),
        );
    }

    renderCover(src) {
        const img = this.els.coverImg;
        if (!src) {
            img.hidden = true;
            img.removeAttribute("src");
            return;
        }
        // Les pochettes locales passent par /art, protégé par le jeton
        const url = src.startsWith("/art") ? `${src}&token=${encodeURIComponent(this.connection.token)}` : src;
        if (img.getAttribute("src") !== url) {
            img.hidden = true;
            img.onload = () => {
                img.hidden = false;
            };
            img.onerror = () => {
                img.hidden = true;
            };
            img.src = url;
        }
    }

    renderPosition() {
        const player = this.current;
        if (!player || this.seeking) {
            return;
        }
        const length = player.metadata.length ?? 0;
        const position = this.position(player);
        this.els.pos.textContent = formatTime(position);
        this.els.len.textContent = length > 0 ? formatTime(length) : "–:––";
        this.els.seek.value = length > 0 ? String(Math.round((position / length) * 1000)) : "0";
        paintRange(this.els.seek);
    }
}

// ----------------------------------------------------------------------
// Panneau son (PipeWire via pactl)
// ----------------------------------------------------------------------

class AudioPanel {
    constructor(connection, i18n) {
        this.connection = connection;
        this.i18n = i18n;
        this.state = null;
        this.dragging = new Set();
        this.throttles = new Map();

        this.els = {
            empty: $("audio-empty"),
            view: $("audio-view"),
            mute: $("master-mute"),
            muteIcon: $("master-mute-icon"),
            volume: $("master-volume"),
            value: $("master-value"),
            name: $("master-name"),
            list: $("sink-list"),
        };

        this.els.mute.addEventListener("click", () => this.command("toggle_mute", this.defaultSink?.name));
        this.bindSlider(this.els.volume, () => this.defaultSink?.name, (value) => {
            this.els.value.textContent = `${value} %`;
        });
    }

    get defaultSink() {
        return this.state?.sinks.find((sink) => sink.name === this.state.default) ?? null;
    }

    command(action, sink, value) {
        this.connection.send({ domain: "audio", action, sink, value });
    }

    throttleFor(sinkName) {
        if (!this.throttles.has(sinkName)) {
            this.throttles.set(
                sinkName,
                new Throttle(80, (value) => this.command("set_volume", sinkName, value)),
            );
        }
        return this.throttles.get(sinkName);
    }

    // Branche un slider de volume : envoi limité en fréquence, pas de mise à jour serveur pendant le geste
    bindSlider(input, sinkName, onValue) {
        input.addEventListener("pointerdown", () => this.dragging.add(input));
        const release = () => this.dragging.delete(input);
        input.addEventListener("pointerup", release);
        input.addEventListener("pointercancel", release);
        input.addEventListener("change", release);
        input.addEventListener("input", () => {
            const name = sinkName();
            paintRange(input);
            onValue?.(input.value);
            if (name) {
                this.throttleFor(name).push(Number(input.value) / 100);
            }
        });
    }

    update(audio) {
        this.state = audio;
        this.els.empty.hidden = audio.available;
        this.els.view.hidden = !audio.available;
        if (!audio.available) {
            return;
        }
        const maxPercent = Math.round((audio.max_volume ?? 1) * 100);
        const sink = this.defaultSink;
        this.els.volume.max = String(maxPercent);
        if (sink) {
            if (!this.dragging.has(this.els.volume)) {
                this.els.volume.value = String(Math.round(sink.volume * 100));
                this.els.value.textContent = `${this.els.volume.value} %`;
                paintRange(this.els.volume);
            }
            this.els.name.textContent = sink.description;
            this.els.muteIcon.setAttribute("href", sink.muted ? "#i-mute" : "#i-volume");
            this.els.mute.classList.toggle("on", sink.muted);
            this.els.volume.classList.toggle("muted", sink.muted);
        }
        this.renderSinks(maxPercent);
    }

    renderSinks(maxPercent) {
        // Ne reconstruit pas la liste pendant un geste : on perdrait le slider en cours
        if ([...this.dragging].some((el) => this.els.list.contains(el))) {
            return;
        }
        const items = this.state.sinks.map((sink) => {
            const li = document.createElement("li");
            li.className = "sink" + (sink.name === this.state.default ? " default" : "");

            const select = document.createElement("button");
            select.type = "button";
            select.className = "sink-select";
            const radio = document.createElement("span");
            radio.className = "radio";
            const label = document.createElement("span");
            label.className = "sink-name";
            label.textContent = sink.description;
            select.append(radio, label);
            if (sink.name === this.state.default) {
                const tag = document.createElement("span");
                tag.className = "tag";
                tag.textContent = this.i18n.t("audio.default");
                select.append(tag);
            }
            select.addEventListener("click", () => this.command("set_default", sink.name));

            const row = document.createElement("div");
            row.className = "slider-row";
            const mute = document.createElement("button");
            mute.type = "button";
            mute.className = "icon-btn small" + (sink.muted ? " on" : "");
            mute.innerHTML = `<svg><use href="${sink.muted ? "#i-mute" : "#i-volume"}"/></svg>`;
            mute.setAttribute("aria-label", this.i18n.t("audio.mute"));
            mute.addEventListener("click", () => this.command("toggle_mute", sink.name));
            const range = document.createElement("input");
            range.type = "range";
            range.min = "0";
            range.max = String(maxPercent);
            range.value = String(Math.round(sink.volume * 100));
            range.setAttribute("aria-label", sink.description);
            const value = document.createElement("span");
            value.className = "value";
            value.textContent = `${range.value} %`;
            this.bindSlider(range, () => sink.name, (v) => {
                value.textContent = `${v} %`;
            });
            paintRange(range);
            row.append(mute, range, value);

            li.append(select, row);
            return li;
        });
        this.els.list.replaceChildren(...items);
    }
}

// ----------------------------------------------------------------------
// Pavé tactile et touches (uinput)
// ----------------------------------------------------------------------

class Touchpad {
    // Seuils des gestes
    static TAP_MAX_MS = 220;
    static TAP_MAX_MOVE = 8;
    static LONG_PRESS_MS = 600;
    static SCROLL_STEP_PX = 28;

    constructor(element, connection) {
        this.element = element;
        this.connection = connection;
        this.pointers = new Map();
        this.moveDx = 0;
        this.moveDy = 0;
        this.scrollAccum = 0;
        this.gesture = null;
        this.frame = null;
        this.longPressTimer = null;

        element.addEventListener("pointerdown", (event) => this.onDown(event));
        element.addEventListener("pointermove", (event) => this.onMove(event));
        element.addEventListener("pointerup", (event) => this.onUp(event));
        element.addEventListener("pointercancel", (event) => this.onUp(event));
        element.addEventListener("contextmenu", (event) => event.preventDefault());
    }

    send(action, payload = {}) {
        this.connection.send({ domain: "input", action, ...payload });
    }

    onDown(event) {
        event.preventDefault();
        this.element.setPointerCapture(event.pointerId);
        this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
        if (this.pointers.size === 1) {
            this.gesture = { start: performance.now(), moved: 0, fingers: 1, longPressed: false };
            this.longPressTimer = setTimeout(() => {
                if (this.gesture && this.gesture.moved < Touchpad.TAP_MAX_MOVE && this.pointers.size === 1) {
                    this.gesture.longPressed = true;
                    this.send("click", { button: "right" });
                    this.element.classList.add("flash");
                    setTimeout(() => this.element.classList.remove("flash"), 150);
                }
            }, Touchpad.LONG_PRESS_MS);
        } else if (this.gesture) {
            this.gesture.fingers = Math.max(this.gesture.fingers, this.pointers.size);
            clearTimeout(this.longPressTimer);
        }
        this.element.classList.add("active");
    }

    onMove(event) {
        const previous = this.pointers.get(event.pointerId);
        if (!previous || !this.gesture) {
            return;
        }
        const dx = event.clientX - previous.x;
        const dy = event.clientY - previous.y;
        this.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
        this.gesture.moved += Math.hypot(dx, dy);

        if (this.pointers.size >= 2) {
            // Défilement à deux doigts : on n'utilise que le premier pointeur
            if (event.pointerId === this.pointers.keys().next().value) {
                this.scrollAccum += dy;
            }
        } else {
            // Accélération simple : les gestes rapides couvrent plus de distance
            const speed = Math.hypot(dx, dy);
            const gain = 1.2 + Math.min(speed / 12, 2.3);
            this.moveDx += dx * gain;
            this.moveDy += dy * gain;
        }
        this.requestFlush();
    }

    onUp(event) {
        this.pointers.delete(event.pointerId);
        if (this.pointers.size > 0 || !this.gesture) {
            return;
        }
        clearTimeout(this.longPressTimer);
        const gesture = this.gesture;
        this.gesture = null;
        this.element.classList.remove("active");
        const quick = performance.now() - gesture.start < Touchpad.TAP_MAX_MS;
        if (!gesture.longPressed && quick && gesture.moved < Touchpad.TAP_MAX_MOVE) {
            this.send("click", { button: gesture.fingers >= 2 ? "right" : "left" });
        }
        this.scrollAccum = 0;
    }

    requestFlush() {
        if (this.frame === null) {
            this.frame = requestAnimationFrame(() => this.flush());
        }
    }

    // Un message par frame au maximum
    flush() {
        this.frame = null;
        const dx = Math.trunc(this.moveDx);
        const dy = Math.trunc(this.moveDy);
        if (dx !== 0 || dy !== 0) {
            this.moveDx -= dx;
            this.moveDy -= dy;
            this.send("move", { dx, dy });
        }
        const steps = Math.trunc(this.scrollAccum / Touchpad.SCROLL_STEP_PX);
        if (steps !== 0) {
            this.scrollAccum -= steps * Touchpad.SCROLL_STEP_PX;
            // Défilement « naturel » : glisser vers le bas fait monter le contenu
            this.send("scroll", { dy: steps });
        }
    }
}

class RemotePanel {
    constructor(connection) {
        this.connection = connection;
        this.touchpad = new Touchpad($("touchpad"), connection);
        document.querySelectorAll("#panel-remote [data-key]").forEach((button) => {
            button.addEventListener("click", () => {
                connection.send({ domain: "input", action: "key", key: button.dataset.key });
            });
        });
    }

    update(input) {
        $("remote-empty").hidden = input.available;
        $("remote-view").hidden = !input.available;
        $("remote-reason").textContent = input.reason || "";
    }
}

// Saisie de texte : le clavier de la tablette remplit le champ, le PC reçoit de vraies frappes
class TextPanel {
    constructor(connection, i18n, toast) {
        this.connection = connection;
        this.i18n = i18n;
        this.toast = toast;
        this.dialog = $("text-dialog");
        this.input = $("text-input");

        $("text-open").addEventListener("click", () => this.open());
        $("text-close").addEventListener("click", () => this.dialog.close());
        $("text-send").addEventListener("click", () => this.send(false));
        // La touche « Envoyer » du clavier tactile valide le formulaire : texte puis Entrée
        $("text-form").addEventListener("submit", (event) => {
            event.preventDefault();
            this.send(true);
        });
        $("text-mask").addEventListener("change", () => {
            this.input.type = $("text-mask").checked ? "password" : "text";
        });
        this.dialog.querySelectorAll("[data-text-key]").forEach((button) => {
            button.addEventListener("click", () => {
                connection.send({ domain: "input", action: "key", key: button.dataset.textKey });
                this.input.focus();
            });
        });
    }

    open() {
        // Non modale : le pavé tactile reste utilisable (cliquer dans un champ du PC, puis taper)
        this.dialog.show();
        this.input.focus();
    }

    // Le serveur traite les messages d'une connexion dans l'ordre : le texte est saisi avant l'Entrée
    send(withEnter) {
        const text = this.input.value;
        if (text) {
            this.connection.send({ domain: "input", action: "text", text });
        }
        if (withEnter) {
            this.connection.send({ domain: "input", action: "key", key: "enter" });
        }
        if (text || withEnter) {
            this.toast.show(this.i18n.t("text.sent"));
        }
        this.input.value = "";
        this.input.focus();
    }

    update(input) {
        const text = input.text ?? { available: false, reason: "" };
        const button = $("text-open");
        button.disabled = !input.available || !text.available;
        button.title = button.disabled ? this.i18n.t("text.unavailable", { reason: text.reason || input.reason }) : "";
        if (button.disabled && this.dialog.open) {
            this.dialog.close();
        }
    }
}

// ----------------------------------------------------------------------
// Panneau système
// ----------------------------------------------------------------------

class HoldButton {
    // Bouton à maintenir : évite une extinction accidentelle depuis la tablette
    static DURATION_MS = 1000;

    constructor(button, onConfirm) {
        this.button = button;
        this.timer = null;
        const start = (event) => {
            event.preventDefault();
            button.classList.add("holding");
            this.timer = setTimeout(() => {
                this.cancel();
                onConfirm();
            }, HoldButton.DURATION_MS);
        };
        button.addEventListener("pointerdown", start);
        button.addEventListener("pointerup", () => this.cancel());
        button.addEventListener("pointerleave", () => this.cancel());
        button.addEventListener("pointercancel", () => this.cancel());
        button.addEventListener("contextmenu", (event) => event.preventDefault());
    }

    cancel() {
        clearTimeout(this.timer);
        this.timer = null;
        this.button.classList.remove("holding");
    }
}

class LauncherPanel {
    // Icônes du sprite (index.html) ; à garder synchronisées avec ICONS de launchers.py
    static ICONS = new Set(["app", "tv", "film", "globe", "music", "screen-off", "speaker", "lock", "window"]);

    constructor(connection, i18n, toast) {
        this.connection = connection;
        this.i18n = i18n;
        this.toast = toast;
        this.key = "";
    }

    update(system) {
        $("launchers-manage").hidden = !system.admin;

        // Reconstruit la grille seulement si la liste a changé
        const key = JSON.stringify(system.launchers);
        if (key === this.key) {
            return;
        }
        this.key = key;
        $("launchers-empty").hidden = system.launchers.length > 0;
        $("launcher-grid").replaceChildren(
            ...system.launchers.map((launcher) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "launcher";
                const icon = LauncherPanel.ICONS.has(launcher.icon) ? launcher.icon : "app";
                button.innerHTML = `<svg><use href="#i-${icon}"/></svg>`;
                const label = document.createElement("span");
                label.textContent = launcher.label;
                button.append(label);
                button.addEventListener("click", () => {
                    this.connection.send({ domain: "system", action: "launch", id: launcher.id });
                    this.toast.show(this.i18n.t("launchers.launched", { name: launcher.label }));
                });
                return button;
            }),
        );
    }
}

// Opérations délicates : verrouillage, veille, redémarrage, extinction
class SystemPanel {
    constructor(connection, i18n) {
        this.i18n = i18n;

        document.querySelectorAll("[data-power]").forEach((button) => {
            new HoldButton(button, () => {
                connection.send({ domain: "system", action: "power", kind: button.dataset.power });
            });
        });
        document.querySelector('[data-system="lock"]').addEventListener("click", () => {
            connection.send({ domain: "system", action: "lock" });
        });
    }

    update(system) {
        document.querySelectorAll("[data-power]").forEach((button) => {
            button.disabled = !system.power;
            button.title = system.power ? "" : this.i18n.t("system.power_unavailable");
        });
    }
}

// ----------------------------------------------------------------------
// Éléments transverses
// ----------------------------------------------------------------------

class Toast {
    constructor(element) {
        this.element = element;
        this.timer = null;
    }

    show(text, isError = false) {
        this.element.textContent = text;
        this.element.classList.toggle("error", isError);
        this.element.hidden = false;
        clearTimeout(this.timer);
        this.timer = setTimeout(() => {
            this.element.hidden = true;
        }, 2500);
    }
}

class Tabs {
    constructor() {
        this.buttons = [...document.querySelectorAll("#tabbar [data-tab]")];
        this.buttons.forEach((button) => {
            button.addEventListener("click", () => this.select(button.dataset.tab));
        });
        this.select(Storage.get(STORAGE_TAB) || "player");
    }

    select(name) {
        if (!this.buttons.some((button) => button.dataset.tab === name)) {
            name = "player";
        }
        this.buttons.forEach((button) => button.classList.toggle("active", button.dataset.tab === name));
        document.querySelectorAll("[data-panel]").forEach((panel) => {
            panel.hidden = panel.dataset.panel !== name;
        });
        Storage.set(STORAGE_TAB, name);
    }
}

class ScreenKeeper {
    // Wake Lock : disponible uniquement en contexte sécurisé (HTTPS ou localhost)
    constructor() {
        this.lock = null;
        if (!("wakeLock" in navigator)) {
            return;
        }
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "visible") {
                this.acquire();
            }
        });
        this.acquire();
    }

    async acquire() {
        try {
            this.lock = await navigator.wakeLock.request("screen");
        } catch {
            this.lock = null;
        }
    }
}

// ----------------------------------------------------------------------
// Application
// ----------------------------------------------------------------------

class App {
    constructor() {
        this.i18n = new I18n(MESSAGES);
        this.i18n.apply();
        this.toast = new Toast($("toast"));
        this.tabs = new Tabs();
        this.screenKeeper = new ScreenKeeper();
        this.lastState = null;

        const token = App.readToken();
        if (!token) {
            $("no-token").hidden = false;
            return;
        }

        this.connection = new Connection(token);
        this.player = new PlayerPanel(this.connection, this.i18n);
        this.audio = new AudioPanel(this.connection, this.i18n);
        this.remote = new RemotePanel(this.connection);
        this.text = new TextPanel(this.connection, this.i18n, this.toast);
        this.launchers = new LauncherPanel(this.connection, this.i18n, this.toast);
        this.system = new SystemPanel(this.connection, this.i18n);

        this.bindTopbar();
        this.connection.addEventListener("state", (event) => this.onState(event.detail));
        this.connection.addEventListener("error", (event) => {
            this.toast.show(this.i18n.t("error.prefix", { message: event.detail.message }), true);
        });
        this.connection.addEventListener("status", (event) => this.onStatus(event.detail));
        this.i18n.onChange(() => {
            this.updateLangButton();
            if (this.lastState) {
                this.launchers.key = "";
                this.onState(this.lastState);
            }
        });
        this.updateLangButton();
        this.connection.connect();
    }

    // Le jeton est lu dans l'URL une fois, mémorisé, puis retiré de la barre d'adresse
    static readToken() {
        const params = new URLSearchParams(location.search);
        const fromUrl = params.get("token");
        if (fromUrl) {
            Storage.set(STORAGE_TOKEN, fromUrl);
            history.replaceState(null, "", location.pathname);
            return fromUrl;
        }
        return Storage.get(STORAGE_TOKEN);
    }

    bindTopbar() {
        $("lang-toggle").addEventListener("click", () => this.i18n.toggle());
        $("fullscreen").addEventListener("click", () => {
            if (document.fullscreenElement) {
                document.exitFullscreen?.();
            } else {
                document.documentElement.requestFullscreen?.().catch(() => {});
            }
        });
    }

    updateLangButton() {
        // Le bouton affiche la langue vers laquelle on bascule
        $("lang-toggle").textContent = this.i18n.lang === "fr" ? "EN" : "FR";
    }

    onStatus(status) {
        $("status").dataset.state = status;
        const text = $("status-text");
        text.dataset.i18n = `status.${status}`;
        text.textContent = this.i18n.t(`status.${status}`);
    }

    onState(state) {
        this.lastState = state;
        this.player.update(state.mpris);
        this.audio.update(state.audio);
        this.remote.update(state.input);
        this.text.update(state.input);
        this.launchers.update(state.system);
        this.system.update(state.system);
    }
}

new App();
