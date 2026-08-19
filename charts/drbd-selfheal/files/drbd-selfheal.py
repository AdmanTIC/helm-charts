#!/usr/bin/env python3
"""Balayeur des resynchronisations DRBD calées et des bitmaps périmés.

Deux défauts établis sur DRBD 9.3.3 :

  1. RESYNCHRONISATION CALÉE — le battement de rôle d'un client diskless invalide la
     resynchronisation en cours ; une fois le battement fini, elle ne repart JAMAIS.
     Signature : replication:SyncTarget, rs-in-flight:0, `received` figé, out-of-sync > 0,
     disque local Inconsistent, connexion Connected.
  2. BITMAP PÉRIMÉ — séquelle du précédent : la paire restée calée garde un bitmap sale
     alors que LES DEUX côtés affichent UpToDate. LINSTOR est tout vert. Si le troisième
     nœud tombe, les deux restants se croient d'accord sans l'être.

Déblocage, dans les deux cas : forcer la renégociation de la paire depuis la CIBLE (le nœud
qui porte le bitmap sale), par `drbdadm disconnect` puis `connect`. À la reconnexion DRBD
compare les UUID courants et, s'ils concordent, purge le bitmap sans transférer un octet.

POURQUOI `exec` DANS LES SATELLITES, ET NON PROMETHEUS. Relevé sur `drbd-reactor` 1.12.0 :
28 métriques exportées, dont AUCUNE ne porte l'état de RÉPLICATION ni l'état de disque du
PAIR. `SyncTarget` est donc invisible depuis Prometheus.
L'API REST de LINSTOR ne donne, par connexion, que `{connected, message}`, et ses propres
métriques ne valent pas mieux. Seul `drbdsetup status --json` porte `replication-state`,
`peer-disk-state`, `resync-suspended` et `has-online-verify-details` — ce dernier permettant
d'écarter un `drbdadm verify` légitime, qui salit le bitmap entre deux pairs UpToDate sans
qu'il y ait le moindre défaut.

POURQUOI LA BIBLIOTHÈQUE STANDARD SEULE : l'API `exec` de Kubernetes réclame une bascule
WebSocket, absente d'`urllib`. Elle tient en une centaine de lignes de `socket` + `ssl`
(classe `Kube`), ce qui évite d'exiger une image porteuse de `kubectl` ou une installation de
paquets à chaque exécution — le job tourne 288 fois par jour.

FORME. CronJob de balayage, et non démon : chaque exécution échantillonne pendant
SAMPLE_WINDOW secondes (plusieurs relevés), puis compare son verdict à celui de l'exécution
précédente, conservé dans un ConfigMap. La preuve de stagnation se construit donc SUR
PLUSIEURS EXÉCUTIONS.

L'invariant qui rend les 4 minutes aveugles entre deux jobs inoffensives : on compare la
valeur ABSOLUE de `received`, pas un débit. Identique à deux exécutions d'écart, elle prouve
que rien n'a circulé dans l'intervalle ET qu'aucune reconnexion n'a eu lieu — une
reconnexion remettrait ce compteur à zéro. Pour le bitmap périmé, où `received` avance
légitimement (réplication normale), l'invariant est `out-of-sync`, constant entre deux pairs
UpToDate connectés puisque les écritures y sont acquittées de façon synchrone.

MODE OBSERVATION. `DRY_RUN=true` (défaut) : le balayeur détecte, journalise, publie ses
métriques et n'exécute AUCUN `drbdadm`. On l'arme après avoir vu ses décisions sur des cas
réels.
"""

import base64
import json
import re
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

# Canaux de la sous-couche `v4.channel.k8s.io` : le premier octet de chaque message
# WebSocket désigne le flux auquel il appartient.
CHAN_STDOUT = 1
CHAN_STDERR = 2
CHAN_ERROR = 3


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"FATAL: variable d'environnement {name} absente")
    return value


def env_bool(name, default="false"):
    return env(name, default).strip().lower() in ("1", "true", "yes", "oui")


def env_int(name, default):
    return int(env(name, str(default)))


CFG = {
    "namespace": env("PIRAEUS_NAMESPACE", "piraeus-datastore"),
    "satellite_selector": env(
        "SATELLITE_SELECTOR", "app.kubernetes.io/component=linstor-satellite"
    ),
    "satellite_container": env("SATELLITE_CONTAINER", "linstor-satellite"),
    # Fenêtre d'échantillonnage INTERNE au job, et nombre de relevés qui s'y répartissent.
    # 60 s est généreux : à `c-min-rate=51200k`, une resynchronisation saine déplace 3 Gio en
    # 60 s, et même à 1 % de ce plancher elle en déplace 30 Mio — elle ne peut pas paraître
    # figée. Les 5 relevés protègent d'une lecture aberrante isolée.
    "sample_window": env_int("SAMPLE_WINDOW_SECONDS", 60),
    "sample_count": env_int("SAMPLE_COUNT", 5),
    # Budget que le script s'impose, sous `activeDeadlineSeconds` : il préfère sortir
    # proprement — donc en journalisant et en publiant — plutôt que d'être tué par le kubelet.
    "budget": env_int("JOB_BUDGET_SECONDS", 105),
    "exec_timeout": env_int("EXEC_TIMEOUT_SECONDS", 8),
    # Seuils de déclenchement. Les DEUX conditions valent : nombre d'exécutions concordantes
    # ET durée de stagnation réellement prouvée. Cette seconde condition rend le calibrage
    # honnête si un cycle a été manqué.
    "stalled_min_streak": env_int("STALLED_MIN_STREAK", 2),
    "stalled_min_proven": env_int("STALLED_MIN_PROVEN_SECONDS", 300),
    "stale_min_streak": env_int("STALE_BITMAP_MIN_STREAK", 4),
    "stale_min_proven": env_int("STALE_BITMAP_MIN_PROVEN_SECONDS", 900),
    # Défauts sur lesquels le balayeur a le droit d'agir, une fois armé.
    "act_on_stalled": env_bool("ACT_ON_STALLED_RESYNC", "true"),
    "act_on_stale": env_bool("ACT_ON_STALE_BITMAP", "true"),
    # 🛑 DÉFAUT 2 : LA RENÉGOCIATION PURGE LE BITMAP, DONC EFFACE LA TRACE D'UNE ÉVENTUELLE
    # DIVERGENCE RÉELLE. Une vérification en ligne est lancée APRÈS l'action pour la
    # reconstituer : le bitmap étant purgé, elle repart propre et redécouvre ce qui diverge
    # vraiment. Vérifier AVANT ne prouverait rien — voir `lancer_verification`, où la mesure
    # qui l'établit est consignée.
    "verify_after_act": env_bool("VERIFY_AFTER_ACT", "true"),
    # Détection des resources orphelines — portées par le noyau, oubliées de LINSTOR.
    # Constat seul, JAMAIS d'action : `drbdsetup down` sur une resource réellement utilisée
    # couperait un volume vivant.
    "detect_orphans": env_bool("DETECT_ORPHAN_RESOURCES", "true"),
    # Au-delà de cet âge, un verdict de vérification ne prouve plus rien : on revérifie.
    "verify_result_max_age": env_int("VERIFY_RESULT_MAX_AGE_SECONDS", 3600),
    # Une vérification qui ne conclut pas dans ce délai est abandonnée, et la paire reste
    # retenue : jamais d'action sur une preuve absente.
    "verify_deadline": env_int("VERIFY_DEADLINE_SECONDS", 3600),
    # Garde-fous de fréquence.
    "pair_cooldown": env_int("PAIR_COOLDOWN_SECONDS", 3600),
    "global_max_actions": env_int("GLOBAL_MAX_ACTIONS", 3),
    "global_window": env_int("GLOBAL_WINDOW_SECONDS", 600),
    "excluded": [
        r.strip() for r in env("EXCLUDED_RESOURCES", "").split(",") if r.strip()
    ],
    "dry_run": env_bool("DRY_RUN", "true"),
    "state_configmap": env("STATE_CONFIGMAP", "drbd-selfheal-state"),
    "own_namespace": env("POD_NAMESPACE", "piraeus-datastore"),
    "pushgateway": env("PUSHGATEWAY_URL", ""),
    "push_job": env("PUSHGATEWAY_JOB", "drbd-selfheal"),
    # Bascule de mise au point : `http://127.0.0.1:8001` derrière un `kubectl proxy` permet
    # de faire tourner ce script depuis un poste, sans jeton de ServiceAccount.
    "api_url": env("KUBE_API_URL", ""),
}

STARTED = time.time()


def log(level, message, **fields):
    """Une ligne par événement, sur stdout. Les verdicts portent leurs champs en `clé=valeur`
    pour rester lisibles à l'œil ET exploitables au `grep`."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    extra = "".join(f" {k}={v}" for k, v in fields.items())
    print(f"{stamp} {level} {message}{extra}", flush=True)


def remaining():
    return CFG["budget"] - (time.time() - STARTED)


# --- API Kubernetes ---------------------------------------------------------


class Kube:
    """Client minimal : lecture/écriture REST par `urllib`, et `exec` par WebSocket monté à
    la main — `urllib` ne sait pas basculer de protocole."""

    def __init__(self):
        if CFG["api_url"]:
            # Mode mise au point derrière `kubectl proxy` : pas de TLS, pas de jeton.
            parsed = urllib.parse.urlparse(CFG["api_url"])
            self.scheme = parsed.scheme
            self.host = parsed.hostname
            self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
            self.token = ""
            self.ctx = None
        else:
            self.scheme = "https"
            self.host = env("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
            self.port = env_int("KUBERNETES_SERVICE_PORT", 443)
            with open(f"{SA_DIR}/token", encoding="utf-8") as handle:
                self.token = handle.read().strip()
            self.ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")

    @property
    def base(self):
        return f"{self.scheme}://{self.host}:{self.port}"

    def _headers(self, request):
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        return request

    def get(self, path):
        request = self._headers(urllib.request.Request(f"{self.base}{path}"))
        with urllib.request.urlopen(request, context=self.ctx, timeout=15) as resp:
            return json.loads(resp.read())

    def merge_patch(self, path, payload):
        request = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(payload).encode(), method="PATCH"
        )
        self._headers(request)
        request.add_header("Content-Type", "application/merge-patch+json")
        with urllib.request.urlopen(request, context=self.ctx, timeout=15) as resp:
            return json.loads(resp.read())

    # --- exec par WebSocket -------------------------------------------------

    def _connect_ws(self, path):
        raw = socket.create_connection((self.host, self.port), timeout=CFG["exec_timeout"])
        if self.ctx is not None:
            raw = self.ctx.wrap_socket(raw, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "Sec-WebSocket-Protocol: v4.channel.k8s.io",
        ]
        if self.token:
            lines.append(f"Authorization: Bearer {self.token}")
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        # En-têtes de réponse : lecture octet par octet jusqu'à la ligne vide, pour ne pas
        # avaler le début de la première trame WebSocket.
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = raw.recv(1)
            if not chunk:
                raise RuntimeError("connexion fermée pendant la bascule WebSocket")
            header += chunk
        status = header.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status:
            raw.close()
            # Un 403 nu ne dit RIEN de sa cause, et il n'en a pratiquement qu'une ici : la
            # bascule WebSocket est un `GET`, or Kubernetes dérive le verbe RBAC de la méthode
            # HTTP sur les sous-ressources `connect`. Un Role n'accordant que `create` sur
            # `pods/exec` — le verbe conventionnel, celui de kubectl, qui passe par POST —
            # refuse donc ce `GET`. Constaté au premier déploiement réel, sur tous les nœuds
            # à la fois.
            if " 403 " in status:
                raise RuntimeError(
                    f"bascule WebSocket refusée : {status} — il manque très probablement le "
                    "verbe `get` sur `pods/exec` : le WebSocket est un GET, `create` ne suffit "
                    "pas. Verifier avec `kubectl auth can-i get pods --subresource=exec "
                    "--as=system:serviceaccount:<ns>:drbd-selfheal -n <ns>`"
                )
            raise RuntimeError(f"bascule WebSocket refusée : {status}")
        return raw

    @staticmethod
    def _read_exact(sock, count):
        buf = b""
        while len(buf) < count:
            chunk = sock.recv(count - len(buf))
            if not chunk:
                raise RuntimeError("connexion fermée en cours de trame")
            buf += chunk
        return buf

    def _read_frames(self, sock):
        """Rend {canal: octets}. Les trames du serveur ne sont jamais masquées ; seule la
        PREMIÈRE trame d'un message porte l'octet de canal, d'où le suivi de `channel`."""
        streams = {}
        channel = None
        while True:
            first, second = self._read_exact(sock, 2)
            opcode = first & 0x0F
            final = bool(first & 0x80)
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(self._read_exact(sock, 2), "big")
            elif length == 127:
                length = int.from_bytes(self._read_exact(sock, 8), "big")
            payload = self._read_exact(sock, length) if length else b""

            if opcode == 0x8:  # close
                return streams
            if opcode == 0x9:  # ping → pong, masqué comme l'exige la RFC côté client
                mask = os.urandom(4)
                masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask + masked)
                continue
            if opcode == 0xA:  # pong
                continue

            if opcode in (0x1, 0x2):  # nouveau message
                if not payload:
                    continue
                channel, payload = payload[0], payload[1:]
            elif opcode != 0x0 or channel is None:  # continuation orpheline
                continue
            streams[channel] = streams.get(channel, b"") + payload
            if final:
                channel = None

    def exec_pod(self, pod, argv):
        """Exécute `argv` dans le conteneur satellite et rend (stdout, stderr)."""
        query = [("container", CFG["satellite_container"]), ("stdout", "true"), ("stderr", "true")]
        query += [("command", arg) for arg in argv]
        path = (
            f"/api/v1/namespaces/{CFG['namespace']}/pods/"
            f"{urllib.parse.quote(pod)}/exec?{urllib.parse.urlencode(query)}"
        )
        sock = self._connect_ws(path)
        try:
            sock.settimeout(CFG["exec_timeout"])
            streams = self._read_frames(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass
        failure = streams.get(CHAN_ERROR, b"").decode(errors="replace").strip()
        if failure:
            try:
                status = json.loads(failure)
            except ValueError:
                status = {}
            if status.get("status") == "Failure":
                raise RuntimeError(status.get("message") or failure)
        return (
            streams.get(CHAN_STDOUT, b"").decode(errors="replace"),
            streams.get(CHAN_STDERR, b"").decode(errors="replace"),
        )

    # --- objets ------------------------------------------------------------

    def satellite_pods(self):
        selector = urllib.parse.quote(CFG["satellite_selector"])
        items = self.get(
            f"/api/v1/namespaces/{CFG['namespace']}/pods?labelSelector={selector}"
        ).get("items", [])
        pods = []
        for item in items:
            if (item.get("status") or {}).get("phase") != "Running":
                continue
            ready = {
                c.get("name"): c.get("ready")
                for c in (item.get("status") or {}).get("containerStatuses") or []
            }
            if not ready.get(CFG["satellite_container"]):
                continue
            pods.append(
                (item["metadata"]["name"], (item.get("spec") or {}).get("nodeName", "?"))
            )
        return sorted(pods, key=lambda p: p[1])

    def read_state(self):
        path = (
            f"/api/v1/namespaces/{CFG['own_namespace']}/configmaps/"
            f"{CFG['state_configmap']}"
        )
        try:
            raw = (self.get(path).get("data") or {}).get("state.json") or "{}"
            state = json.loads(raw)
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as err:
            log("WARN", "etat illisible, on repart de zero", erreur=type(err).__name__)
            state = {}
        state.setdefault("pairs", {})
        state.setdefault("actions", [])
        state.setdefault("counters", {})
        # Vérifications en ligne en cours ou conclues, par paire. Survit à la remise à zéro
        # des séries : une paire perd sa série pendant sa vérification — `repl` vaut
        # `VerifyS`/`VerifyT`, donc la classification ne rend plus de verdict — et doit se
        # requalifier après. La PREUVE, elle, doit traverser cette remise à zéro.
        state.setdefault("probes", {})
        return state

    def write_state(self, state):
        """Un échec d'écriture n'est PAS fatal : la série repartira de zéro à la prochaine
        exécution, ce qui ne fait que retarder une action — jamais en provoquer une."""
        state["updated"] = int(time.time())
        # Les 50 dernières actions suffisent au garde-fou de fréquence et au diagnostic ;
        # au-delà, le ConfigMap grossirait sans fin.
        state["actions"] = state["actions"][-50:]
        path = (
            f"/api/v1/namespaces/{CFG['own_namespace']}/configmaps/"
            f"{CFG['state_configmap']}"
        )
        try:
            self.merge_patch(path, {"data": {"state.json": json.dumps(state, indent=1)}})
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as err:
            log("WARN", "etat non enregistre, series remises a zero", erreur=str(err)[:160])


# --- Lecture de l'état DRBD -------------------------------------------------


def parse_status(node, payload):
    """Transforme un `drbdsetup status --json` en observations par paire.

    Clé d'une paire : (nœud local, resource, volume, pair). `out-of-sync` est ASYMÉTRIQUE :
    c'est le nœud qui porte le bitmap sale qui doit agir, donc celui d'où l'on lit.
    """
    pairs = {}
    resources = {}
    for res in payload:
        name = res.get("name")
        if not name:
            continue
        devices = {
            dev.get("volume"): dev for dev in res.get("devices") or [] if "volume" in dev
        }
        conn_states = {}
        for conn in res.get("connections") or []:
            peer = conn.get("name")
            conn_states[peer] = conn.get("connection-state")
            for pdev in conn.get("peer_devices") or []:
                volume = pdev.get("volume")
                local = devices.get(volume) or {}
                pairs[(node, name, volume, peer)] = {
                    "node": node,
                    "resource": name,
                    "volume": volume,
                    "peer": peer,
                    "disk": local.get("disk-state"),
                    "client": bool(local.get("client")),
                    "quorum": bool(local.get("quorum")),
                    "conn": conn.get("connection-state"),
                    "congested": bool(conn.get("congested")),
                    "rs_in_flight": conn.get("rs-in-flight"),
                    "repl": pdev.get("replication-state"),
                    "peer_disk": pdev.get("peer-disk-state"),
                    "peer_client": bool(pdev.get("peer-client")),
                    "resync_suspended": str(pdev.get("resync-suspended")),
                    "received": pdev.get("received"),
                    # `drbdsetup` compte en Kio ; on convertit en octets pour parler la même
                    # langue que `drbd_peerdevice_outofsync_bytes`.
                    "out_of_sync": (pdev.get("out-of-sync") or 0) * 1024,
                    "verify": bool(pdev.get("has-online-verify-details")),
                }
        resources[(node, name)] = {
            "connections": conn_states,
            "role": res.get("role"),
            "suspended": bool(res.get("suspended")),
        }
    return pairs, resources


def collect(kube, pods):
    """Échantillonne `sample_count` fois sur `sample_window`. Rend (relevés, resources, erreurs).

    Le coût des `exec` se fond dans l'attente : un tour de 12 satellites coûte ~5 s (0,42 s
    mesuré par appel), pour une cadence de 15 s. Balayer TOUS les satellites plutôt que les
    seuls nœuds storage ne coûte donc rien, et reste juste si une réplique diskful atterrit
    un jour ailleurs — un nœud diskless ne produit simplement aucune paire.
    """
    count = max(2, CFG["sample_count"])
    spacing = CFG["sample_window"] / (count - 1)
    samples, resources, errors = [], {}, 0

    for index in range(count):
        if index and remaining() < spacing + 10:
            log("WARN", "budget epuise, echantillonnage ecourte", releves=index)
            break
        if index:
            time.sleep(max(0.0, spacing - (time.time() - target)))
        target = time.time()
        reading = {}
        for pod, node in pods:
            if remaining() < 10:
                log("WARN", "budget epuise en cours de tour", noeud=node)
                break
            try:
                out, err = kube.exec_pod(pod, ["drbdsetup", "status", "--json"])
                if not out.strip():
                    raise RuntimeError(err.strip()[:200] or "sortie vide")
                pairs, res = parse_status(node, json.loads(out))
            except (RuntimeError, OSError, ValueError, socket.timeout) as exc:
                errors += 1
                log("WARN", "releve impossible", noeud=node, erreur=str(exc)[:160])
                continue
            reading.update(pairs)
            resources.update(res)
        samples.append({"at": target, "pairs": reading})
    return samples, resources, errors


# --- Classification --------------------------------------------------------


def classify(readings):
    """Rend ("stalled_resync"|"stale_bitmap"|None, empreinte) pour une paire.

    Toutes les conditions doivent tenir dans TOUS les relevés du job : un seul relevé
    discordant écarte la paire. L'empreinte sert à valider la continuité entre exécutions.
    """
    if len(readings) < 2:
        return None, None

    def same(field):
        return len({r[field] for r in readings}) == 1

    def every(predicate):
        return all(predicate(r) for r in readings)

    first = readings[0]

    # Conditions communes aux deux défauts.
    if not every(lambda r: r["conn"] == "Connected"):
        return None, None
    if not every(lambda r: r["out_of_sync"] > 0):
        return None, None
    # Un device local diskless n'a pas de bitmap : le défaut ne s'applique pas, ce n'est pas
    # une action refusée. Le QUORUM, lui, est délibérément absent d'ici : c'est un garde-fou,
    # pas un critère de détection — sans quoi une paire fautive sur un volume ayant perdu son
    # quorum deviendrait INVISIBLE, au lieu d'apparaître en « détectée, action refusée ».
    if not every(lambda r: not r["client"]):
        return None, None
    # Un `drbdadm verify` salit légitimement le bitmap entre deux pairs UpToDate.
    if not every(lambda r: not r["verify"]):
        return None, None
    if not every(lambda r: r["resync_suspended"] == "no"):
        return None, None
    if not every(lambda r: r["rs_in_flight"] == 0):
        return None, None
    if not (same("disk") and same("peer_disk") and same("repl")):
        return None, None

    # Défaut 1 : la cible est Inconsistent et `received` ne bouge plus. L'empreinte porte la
    # valeur ABSOLUE de `received` : identique d'une exécution à l'autre, elle prouve à la
    # fois l'absence de trafic et l'absence de reconnexion.
    if (
        first["disk"] == "Inconsistent"
        and every(lambda r: r["repl"] == "SyncTarget")
        and every(lambda r: not r["congested"])
        and same("received")
    ):
        return "stalled_resync", f"stalled|{first['peer_disk']}|{first['received']}"

    # Défaut 2 : deux pairs UpToDate, aucune resynchronisation, bitmap non vide. Ici
    # `received` avance légitimement (réplication normale) ; l'invariant est `out-of-sync`,
    # constant entre deux pairs connectés dont les écritures sont acquittées.
    if (
        first["disk"] == "UpToDate"
        and first["peer_disk"] == "UpToDate"
        and every(lambda r: r["repl"] == "Established")
        and every(lambda r: not r["peer_client"])
        and same("out_of_sync")
    ):
        return "stale_bitmap", f"stale|{first['out_of_sync']}"

    return None, None


# --- Garde-fous ------------------------------------------------------------


def guard(key, defect, reading, resources, state, acted_resources, now):
    """Rend le motif de refus, ou None si l'action est permise.

    « Ne jamais agir sur une paire dont la cible n'est pas Inconsistent ET dont le quorum ne
    tient pas ailleurs » : couper une connexion doit rester sans effet sur la disponibilité.
    """
    node, resource, _volume, peer = key

    if resource in CFG["excluded"]:
        return "resource_exclue"
    if defect == "stalled_resync" and not CFG["act_on_stalled"]:
        return "defaut_hors_perimetre"
    if defect == "stale_bitmap" and not CFG["act_on_stale"]:
        return "defaut_hors_perimetre"
    if not reading["quorum"]:
        return "quorum_local_perdu"

    info = resources.get((node, resource)) or {}
    if info.get("suspended"):
        return "resource_suspendue"

    # Une resource en cours de rétablissement est hors limites : toute autre connexion doit
    # être Connected avant qu'on en coupe une.
    autres = {p: s for p, s in (info.get("connections") or {}).items() if p != peer}
    if any(s != "Connected" for s in autres.values()):
        return "autre_connexion_degradee"

    # Après la coupure, il doit rester au moins un pair diskful UpToDate connecté : c'est ce
    # qui garantit que le quorum tient ailleurs.
    survivants = 0
    for (n, res, _vol, other), obs in reading["_siblings"].items():
        if (n, res) != (node, resource) or other == peer:
            continue
        if obs["conn"] == "Connected" and obs["peer_disk"] == "UpToDate" and not obs["peer_client"]:
            survivants += 1
    if survivants < 1:
        return "aucun_pair_sain_restant"

    # DRBD ne resynchronise que depuis une source à la fois : une autre resynchronisation qui
    # progresse sur la même resource explique la stagnation sans qu'il y ait défaut.
    for (n, res, _vol, other), obs in reading["_siblings"].items():
        if (n, res) != (node, resource) or other == peer:
            continue
        if obs["repl"] in ("SyncTarget", "SyncSource") and (obs["rs_in_flight"] or 0) > 0:
            return "autre_resync_en_cours"

    if resource in acted_resources:
        return "une_paire_a_la_fois"

    previous = [a for a in state["actions"] if a.get("key") == "|".join(map(str, key))]
    if previous and now - previous[-1]["ts"] < CFG["pair_cooldown"]:
        return "delai_de_garde_paire"

    recent = [a for a in state["actions"] if now - a.get("ts", 0) < CFG["global_window"]]
    if len(recent) >= CFG["global_max_actions"]:
        return "plafond_global"

    return None


# --- Action ----------------------------------------------------------------


HORODATAGE_NOYAU = re.compile(r"^\[\s*(\d+\.\d+)\]\s*(.*)$")
BLOCS_DIVERGENTS = re.compile(r"Online verify found (\d+) 4k blocks out of sync")


def lignes_verification(kube, pod, resource, peer):
    """Rend les lignes « Online verify » du journal noyau visant cette paire, horodatées.

    Le filtrage grossier a lieu DANS le conteneur : un `dmesg` entier peut peser plusieurs
    mégaoctets alors que la fenêtre d'exécution est de quelques secondes. La chaîne confiée au
    shell est CONSTANTE — ni le nom de resource ni celui du pair n'y entrent, le tri fin se
    fait ici. Aucune donnée ne traverse donc un interpréteur.

    L'horodatage est celui du noyau, en secondes depuis le démarrage : monotone, donc
    comparable d'une exécution du balayeur à l'autre sans dépendre de l'heure du système.
    """
    try:
        out, _ = kube.exec_pod(pod, ["sh", "-c", "dmesg | grep 'Online verify' | tail -40"])
    except (RuntimeError, OSError) as erreur:
        # `grep` sans correspondance rend 1, ce qui remonte ici : c'est un journal SANS ligne
        # de vérification, pas une panne. Dans les deux cas la réponse est la même — aucun
        # relevé, donc aucune conclusion, donc aucune action.
        log("INFO", "journal noyau sans releve de verification", erreur=str(erreur)[:120])
        return []
    releves = []
    for ligne in (out or "").splitlines():
        trouve = HORODATAGE_NOYAU.match(ligne.strip())
        if not trouve:
            continue
        texte = trouve.group(2)
        # Forme de la ligne : `drbd <resource>/<vol> <device> <pair>: Online verify ...`
        if resource not in texte or f" {peer}:" not in texte:
            continue
        releves.append((float(trouve.group(1)), texte))
    releves.sort()
    return releves


def verdict_verification(kube, pod, resource, peer, depuis):
    """Rend (« propre » | « divergent » | None, nombre de blocs) pour la vérification lancée
    après l'horodatage `depuis`. None signifie « pas encore conclu ».

    🛑 LE NOYAU N'ÉCRIT « Online verify found N 4k blocks out of sync! » QUE LORSQU'IL EN A
    TROUVÉ. Une vérification propre ne rend qu'« Online verify done ». Relevé sur DRBD 9.3.3,
    sur les deux cas :

        Online verify done (total 241 sec; paused 0 sec; 43516 K/sec)
        Online verify done (total 243 sec; paused 0 sec; 43156 K/sec)
        Online verify found 6 4k blocks out of sync!

    ⚠️ Les blocs « skipped » NE SONT PAS des divergences — « Online verify done but 2 4k
    blocks skipped » signale des blocs trop occupés pour être comparés, pas des blocs
    différents. Les compter comme divergents retiendrait l'action à tout jamais sur un volume
    en écriture soutenue.
    """
    nouvelles = [
        (h, t) for h, t in lignes_verification(kube, pod, resource, peer) if h > depuis
    ]
    divergents = 0
    conclu = False
    for _, texte in nouvelles:
        trouve = BLOCS_DIVERGENTS.search(texte)
        if trouve:
            divergents += int(trouve.group(1))
        if "Online verify done" in texte:
            conclu = True
    if not conclu:
        return None, divergents
    return ("divergent" if divergents else "propre"), divergents


def cle_sonde(key):
    """Clé de sonde : une vérification en ligne porte sur une CONNEXION, pas sur un sens.

    🛑 SANS CETTE NORMALISATION, LA SONDE SE LANCE DEUX FOIS ET LA SECONDE ÉCHOUE. Un bitmap
    périmé est vu des DEUX côtés : `(nodeA, peer=nodeB)` et `(nodeB, peer=nodeA)` sont deux
    clés de paire distinctes qui rendent le même verdict. La seconde relançait `drbdadm verify`
    sur une connexion déjà en cours de vérification, que DRBD refuse avec le code 11. Constaté
    à l'essai. En triant les deux nœuds, les deux sens
    partagent une seule sonde : le premier lance, le second constate.
    """
    node, resource, volume, peer = key
    a, b = sorted((str(node), str(peer)))
    return f"{resource}|{volume}|{a}|{b}"


# LINSTOR dépose la configuration DRBD de chaque resource qu'il pilote sur un nœud dans ce
# répertoire, un fichier `<resource>.res` par resource. C'est CE répertoire que `drbdadm` lit.
CONFIGS_LINSTOR = "/var/lib/linstor.d"


def relever_orphelines(kube, pods, resources):
    """Rend les resources portées par le noyau dont LINSTOR a oublié la configuration.

    🛑 CE CAS ARRIVE, ET IL PRODUIT UNE PANNE D'ATTACHEMENT LATENTE. Constaté : la suppression
    d'une ligne interne de la base LINSTOR laisse sur un nœud une resource DRBD encore présente
    dans le NOYAU, mais dont LINSTOR ne sait plus rien. Plus personne ne la réconcilie :
    `StandAlone` vers ses pairs, sans quorum, E/S suspendues, pendant des heures. Rien ne la
    nomme — les alertes de santé se déclenchent sur ses SYMPTÔMES, aucune ne dit « cette
    resource n'appartient à personne ».

    L'enjeu dépasse le bruit : si un pod est planifié sur ce nœud, le CSI y demande la création
    d'une resource du même nom, et ce fantôme s'y oppose.

    ⚠️ Le signal est la DISPARITION DU FICHIER DE CONFIGURATION, et c'est le bon : c'est
    exactement ce que `drbdadm` lit, d'où son refus `not defined in your config (for this
    host)` — l'outil habituel ne peut plus voir ce qu'il faut retirer. Le retrait se fait donc
    par `drbdsetup down`, qui agit sur l'objet noyau sans configuration.

    🛑 UN RELEVÉ ILLISIBLE NE CONCLUT JAMAIS. Si la liste des configurations est vide ou
    inexploitable alors que le noyau porte des resources, on n'en déduit RIEN : conclure
    déclarerait orphelines TOUTES les resources du nœud d'un coup. Même règle que partout
    ailleurs ici — en cas de doute, on se taît.
    """
    portees = {}
    for noeud, resource in resources:
        portees.setdefault(noeud, set()).add(resource)

    orphelines = []
    for pod, noeud in pods:
        au_noyau = portees.get(noeud) or set()
        if not au_noyau:
            continue
        try:
            out, _ = kube.exec_pod(
                pod, ["sh", "-c", f"ls {CONFIGS_LINSTOR}/*.res 2>/dev/null"]
            )
        except (RuntimeError, OSError) as erreur:
            log(
                "WARN",
                "configurations LINSTOR illisibles, aucun verdict d'orpheline",
                noeud=noeud,
                erreur=str(erreur)[:160],
            )
            continue
        declarees = {
            ligne.strip().rsplit("/", 1)[-1][: -len(".res")]
            for ligne in (out or "").splitlines()
            if ligne.strip().endswith(".res")
        }
        if not declarees:
            log(
                "WARN",
                "aucune configuration LINSTOR relevee, aucun verdict d'orpheline",
                noeud=noeud,
                au_noyau=len(au_noyau),
            )
            continue
        for resource in sorted(au_noyau - declarees):
            log(
                "ERROR",
                "RESOURCE ORPHELINE : portee par le noyau, inconnue de LINSTOR",
                noeud=noeud,
                resource=resource,
                retrait=f"drbdsetup down {resource}",
            )
            orphelines.append({"node": noeud, "resource": resource})
    return orphelines


def lancer_verification(kube, pod, state, key, now):
    """Lance une vérification en ligne APRÈS une renégociation, et enregistre sa sonde.

    🛑 L'ORDRE EST CELUI-LÀ, ET C'EST UN RÉSULTAT DE MESURE, PAS UNE PRÉFÉRENCE DE STYLE.

    Vérifier AVANT de purger ne prouve rien. Éprouvé sur un volume d'essai : une
    vérification lancée sur un bitmap DÉJÀ sale ré-annonce les bits posés. Deux vérifications
    successives ont rendu « Online verify found 16 4k blocks out of sync! » alors que les
    empreintes md5 des deux répliques étaient IDENTIQUES sur la région visée — la divergence
    avait été réparée sous DRBD entre les deux. Le signal `found N` ne vaut donc que si le
    bitmap était PROPRE avant la vérification.

    Or le défaut 2 est, par définition, un bitmap déjà sale. Une barrière préalable aurait donc
    refusé indéfiniment et rendu le composant inerte sur le défaut le plus fréquent.

    Après la renégociation, le bitmap est purgé : la TRACE est perdue, pas les DONNÉES. Une
    vérification repart alors d'un bitmap propre, redécouvre une divergence réelle et la
    remarque correctement. La trace est reconstituée en quelques minutes au lieu d'être perdue
    pour toujours, et l'alerte `DrbdSelfHealDivergenceReelle` la porte à la connaissance d'un
    humain — car la vérification DÉTECTE, elle ne répare pas.
    """
    node, resource, volume, peer = key
    state["probes"][cle_sonde(key)] = {
        "a_lancer": True,
        "demande": now,
        "noeud": node,
        "resource": resource,
        "volume": volume,
        "pair": peer,
    }
    log(
        "INFO",
        "VERIFICATION posterieure programmee",
        resource=resource,
        noeud=node,
        pair=peer,
    )


def _lancer(kube, pod, sonde, now):
    """Lance le `drbdadm verify` d'une sonde en attente. Rend True si elle est partie.

    La renégociation vient de se produire : la connexion peut encore être en `Connecting`, et
    `drbdadm verify` échoue alors. L'échec n'est pas une erreur — la sonde reste en attente et
    repartira au balayage suivant.
    """
    resource, peer = sonde.get("resource") or "", sonde.get("pair") or ""
    try:
        depuis = 0.0
        releves = lignes_verification(kube, pod, resource, peer)
        if releves:
            depuis = releves[-1][0]
        kube.exec_pod(pod, ["drbdadm", "verify", f"{resource}:{peer}"])
    except (RuntimeError, OSError) as erreur:
        log(
            "INFO",
            "VERIFICATION posterieure pas encore lancable, on reessaiera",
            resource=resource,
            pair=peer,
            erreur=str(erreur)[:160],
        )
        return False
    sonde.pop("a_lancer", None)
    sonde.update({"lance": now, "depuis": depuis})
    log(
        "WARN",
        "VERIFICATION lancee apres renegociation",
        resource=resource,
        pair=peer,
        depuis_noyau=f"{depuis:.0f}",
    )
    return True


def relever_verifications(kube, pods, state, now):
    """Relève les vérifications postérieures, et rend les divergences confirmées.

    🛑 CE RELEVÉ EST INDÉPENDANT DES VERDICTS, ET IL DOIT L'ÊTRE. Après une renégociation
    réussie le bitmap est purgé, donc la paire ne produit plus AUCUN verdict — or c'est
    précisément là qu'il faut aller lire le résultat. Un relevé branché sur les verdicts ne
    verrait jamais rien.
    """
    divergences = []
    for cle, sonde in list((state.get("probes") or {}).items()):
        resource = sonde.get("resource") or ""
        peer = sonde.get("pair") or ""
        node = sonde.get("noeud") or ""

        # 🛑 Seul le nœud qui a lancé peut interpréter son propre repère : `depuis` est un
        # horodatage NOYAU, en secondes depuis le démarrage DE CE NŒUD. Lu ailleurs, il ne veut
        # rien dire — les deux extrémités d'une connexion n'ont pas démarré ensemble. Défaut
        # constaté à l'essai : une vieille ligne du journal du pair portait un
        # horodatage supérieur au repère, et passait pour neuve.
        pod = next((p for p, n in pods if n == node), None)
        if pod is None:
            continue

        if sonde.get("verdict") == "divergent":
            if now - sonde.get("conclu", 0) <= CFG["verify_result_max_age"]:
                divergences.append(sonde)
            else:
                state["probes"].pop(cle, None)
            continue

        if sonde.get("a_lancer"):
            if now - sonde.get("demande", now) > CFG["verify_deadline"]:
                log(
                    "WARN",
                    "VERIFICATION posterieure jamais lancable, abandonnee",
                    resource=resource,
                    pair=peer,
                )
                state["probes"].pop(cle, None)
            else:
                _lancer(kube, pod, sonde, now)
            continue

        verdict, blocs = verdict_verification(
            kube, pod, resource, peer, sonde.get("depuis", 0.0)
        )

        if verdict is None:
            if now - sonde.get("lance", now) > CFG["verify_deadline"]:
                log(
                    "WARN",
                    "VERIFICATION posterieure sans reponse, abandonnee",
                    resource=resource,
                    pair=peer,
                )
                state["probes"].pop(cle, None)
            continue

        if verdict == "divergent":
            sonde.update({"verdict": "divergent", "blocs": blocs, "conclu": now})
            log(
                "ERROR",
                "DIVERGENCE REELLE apres renegociation, geste humain requis",
                resource=resource,
                noeud=node,
                pair=peer,
                blocs_4k=blocs,
                octets=blocs * 4096,
            )
            divergences.append(sonde)
            continue

        log(
            "INFO",
            "VERIFICATION posterieure propre, repliques conformes",
            resource=resource,
            pair=peer,
        )
        state["probes"].pop(cle, None)

    return divergences


def renegotiate(kube, pod, resource, peer):
    """`drbdadm disconnect` puis `connect` sur la seule paire visée, depuis la cible.

    Les arguments partent en `argv` par l'API `exec` : aucun interpréteur n'intervient, donc
    le piège de citation de `$R:pair` sous zsh — qui impose des guillemets en ligne de
    commande — ne s'applique pas ici.
    """
    target = f"{resource}:{peer}"
    for verb in ("disconnect", "connect"):
        out, err = kube.exec_pod(pod, ["drbdadm", verb, target])
        detail = (err or out).strip()
        if detail:
            log("INFO", f"drbdadm {verb}", paire=target, sortie=detail[:200])


# --- Publication des métriques ---------------------------------------------


def escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def push(lines):
    """Dépose les métriques sur le Pushgateway. Le groupe entier est REMPLACÉ à chaque
    exécution : une paire rétablie disparaît donc d'elle-même des métriques.

    Un échec de publication n'est pas fatal — le job a déjà journalisé ses verdicts, et la
    péremption du groupe est surveillée par `drbd_selfheal_last_run_timestamp_seconds`.
    """
    if not CFG["pushgateway"]:
        log("INFO", "aucun pushgateway configure, metriques non publiees")
        return
    url = f"{CFG['pushgateway'].rstrip('/')}/metrics/job/{urllib.parse.quote(CFG['push_job'])}"
    body = ("\n".join(lines) + "\n").encode()
    request = urllib.request.Request(url, data=body, method="PUT")
    request.add_header("Content-Type", "text/plain; version=0.0.4")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            resp.read()
        log("INFO", "metriques publiees", lignes=len(lines))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as err:
        log("WARN", "publication des metriques impossible", erreur=str(err)[:160])


def build_metrics(verdicts, state, stats, divergences=(), orphelines=()):
    lines = [
        "# HELP drbd_selfheal_last_run_timestamp_seconds Fin de la derniere execution du balayeur.",
        "# TYPE drbd_selfheal_last_run_timestamp_seconds gauge",
        f"drbd_selfheal_last_run_timestamp_seconds {int(time.time())}",
        "# HELP drbd_selfheal_run_duration_seconds Duree de la derniere execution.",
        "# TYPE drbd_selfheal_run_duration_seconds gauge",
        f"drbd_selfheal_run_duration_seconds {time.time() - STARTED:.2f}",
        "# HELP drbd_selfheal_dry_run Le balayeur est en mode observation (1) ou arme (0).",
        "# TYPE drbd_selfheal_dry_run gauge",
        f"drbd_selfheal_dry_run {1 if CFG['dry_run'] else 0}",
        "# HELP drbd_selfheal_samples_collected Nombre de releves du dernier echantillonnage.",
        "# TYPE drbd_selfheal_samples_collected gauge",
        f"drbd_selfheal_samples_collected {stats['samples']}",
        "# HELP drbd_selfheal_nodes_scanned Satellites interroges.",
        "# TYPE drbd_selfheal_nodes_scanned gauge",
        f"drbd_selfheal_nodes_scanned {stats['nodes']}",
        "# HELP drbd_selfheal_pairs_observed Paires DRBD observees.",
        "# TYPE drbd_selfheal_pairs_observed gauge",
        f"drbd_selfheal_pairs_observed {stats['pairs']}",
        "# HELP drbd_selfheal_exec_errors_total Releves impossibles, cumul.",
        "# TYPE drbd_selfheal_exec_errors_total counter",
        f"drbd_selfheal_exec_errors_total {state['counters'].get('exec_errors', 0)}",
        "# HELP drbd_selfheal_detected Paire reunissant tous les criteres du defaut, seuil atteint.",
        "# TYPE drbd_selfheal_detected gauge",
        "# HELP drbd_selfheal_would_act Paire que le balayeur debloquerait s'il etait arme.",
        "# TYPE drbd_selfheal_would_act gauge",
        "# HELP drbd_selfheal_blocked Paire detectee mais ecartee par un garde-fou.",
        "# TYPE drbd_selfheal_blocked gauge",
        "# HELP drbd_selfheal_streak Executions concordantes accumulees par une paire suspecte.",
        "# TYPE drbd_selfheal_streak gauge",
        "# HELP drbd_selfheal_proven_stagnation_seconds Duree de stagnation reellement prouvee.",
        "# TYPE drbd_selfheal_proven_stagnation_seconds gauge",
        "# HELP drbd_selfheal_out_of_sync_bytes Bitmap divergent vu depuis le noeud qui le porte.",
        "# TYPE drbd_selfheal_out_of_sync_bytes gauge",
        "# HELP drbd_selfheal_actions_total Renegociations effectuees, cumul.",
        "# TYPE drbd_selfheal_actions_total counter",
        "# HELP drbd_selfheal_action_failures_total Renegociations en echec, cumul.",
        "# TYPE drbd_selfheal_action_failures_total counter",
    ]
    for verdict in verdicts:
        tags = (
            f'node="{escape(verdict["node"])}",resource="{escape(verdict["resource"])}",'
            f'volume="{escape(verdict["volume"])}",peer="{escape(verdict["peer"])}",'
            f'defect="{escape(verdict["defect"])}"'
        )
        lines.append(f"drbd_selfheal_streak{{{tags}}} {verdict['streak']}")
        lines.append(
            f"drbd_selfheal_proven_stagnation_seconds{{{tags}}} {verdict['proven']:.0f}"
        )
        lines.append(f"drbd_selfheal_out_of_sync_bytes{{{tags}}} {verdict['out_of_sync']}")
        if not verdict["threshold_reached"]:
            continue
        lines.append(f"drbd_selfheal_detected{{{tags}}} 1")
        if verdict["blocked"]:
            lines.append(
                f'drbd_selfheal_blocked{{{tags},reason="{escape(verdict["blocked"])}"}} 1'
            )
        elif CFG["dry_run"]:
            lines.append(f"drbd_selfheal_would_act{{{tags}}} 1")
    if divergences:
        lines.append(
            "# HELP drbd_selfheal_divergence_reelle Divergence REELLE entre deux repliques "
            "UpToDate, confirmee par une verification posterieure a la renegociation."
        )
        lines.append("# TYPE drbd_selfheal_divergence_reelle gauge")
        lines.append("# TYPE drbd_selfheal_divergence_blocs gauge")
        for sonde in divergences:
            tags = (
                f'node="{escape(sonde.get("noeud"))}",'
                f'resource="{escape(sonde.get("resource"))}",'
                f'volume="{escape(sonde.get("volume"))}",'
                f'peer="{escape(sonde.get("pair"))}"'
            )
            lines.append(f"drbd_selfheal_divergence_reelle{{{tags}}} 1")
            lines.append(
                f"drbd_selfheal_divergence_blocs{{{tags}}} {sonde.get('blocs', 0)}"
            )
    if orphelines:
        lines.append(
            "# HELP drbd_selfheal_resource_orpheline Resource portee par le noyau dont LINSTOR "
            "a oublie la configuration : elle n'est plus reconciliee par personne."
        )
        lines.append("# TYPE drbd_selfheal_resource_orpheline gauge")
        for orpheline in orphelines:
            lines.append(
                "drbd_selfheal_resource_orpheline{"
                f'node="{escape(orpheline["node"])}",'
                f'resource="{escape(orpheline["resource"])}"'
                "} 1"
            )
    for name in ("actions_total", "action_failures_total"):
        for tags, value in sorted(state["counters"].items()):
            if not tags.startswith(f"{name}|"):
                continue
            lines.append(f"drbd_selfheal_{name}{{{tags.split('|', 1)[1]}}} {value}")
    return lines


# --- Boucle principale -----------------------------------------------------


def main():
    kube = Kube()
    pods = kube.satellite_pods()
    if not pods:
        log("ERROR", "aucun pod satellite pret, rien a balayer")
        return 1
    log(
        "INFO",
        "balayage",
        satellites=len(pods),
        fenetre=CFG["sample_window"],
        releves=CFG["sample_count"],
        mode="observation" if CFG["dry_run"] else "arme",
    )

    samples, resources, errors = collect(kube, pods)
    state = kube.read_state()
    state["counters"]["exec_errors"] = state["counters"].get("exec_errors", 0) + errors

    # Une seule exécution utilisable ne prouve rien : on enregistre l'erreur et on sort sans
    # toucher aux séries en cours — un job dégradé ne doit ni agir ni casser un compteur.
    if len(samples) < 2:
        log("ERROR", "echantillonnage insuffisant, aucun verdict", releves=len(samples))
        stats = {"samples": len(samples), "nodes": len(pods), "pairs": 0}
        push(build_metrics([], state, stats))
        kube.write_state(state)
        return 1

    keys = set(samples[0]["pairs"])
    for sample in samples[1:]:
        keys &= set(sample["pairs"])

    now = time.time()
    verdicts, acted_resources = [], set()
    fresh_pairs = {}

    for key in sorted(keys):
        readings = [s["pairs"][key] for s in samples]
        defect, fingerprint = classify(readings)
        marker = "|".join(map(str, key))
        if not defect:
            continue

        history = state["pairs"].get(marker) or {}
        if history.get("fingerprint") == fingerprint and history.get("defect") == defect:
            streak = history.get("streak", 1) + 1
            first_seen = history.get("first_seen", samples[0]["at"])
        else:
            streak, first_seen = 1, samples[0]["at"]
        proven = now - first_seen

        fresh_pairs[marker] = {
            "defect": defect,
            "fingerprint": fingerprint,
            "streak": streak,
            "first_seen": first_seen,
        }

        if defect == "stalled_resync":
            reached = streak >= CFG["stalled_min_streak"] and proven >= CFG["stalled_min_proven"]
        else:
            reached = streak >= CFG["stale_min_streak"] and proven >= CFG["stale_min_proven"]

        latest = dict(readings[-1])
        latest["_siblings"] = samples[-1]["pairs"]
        verdict = {
            "node": key[0],
            "resource": key[1],
            "volume": key[2],
            "peer": key[3],
            "defect": defect,
            "streak": streak,
            "proven": proven,
            "out_of_sync": latest["out_of_sync"],
            "threshold_reached": reached,
            "blocked": None,
            "acted": False,
        }

        if not reached:
            log(
                "INFO",
                "VERDICT suspecte, seuil non atteint",
                defaut=defect,
                noeud=key[0],
                resource=key[1],
                pair=key[3],
                executions=streak,
                prouve=f"{proven:.0f}s",
                out_of_sync=latest["out_of_sync"],
            )
            verdicts.append(verdict)
            continue

        verdict["blocked"] = guard(
            key, defect, latest, resources, state, acted_resources, now
        )
        if verdict["blocked"]:
            log(
                "WARN",
                "VERDICT detecte, action refusee",
                defaut=defect,
                noeud=key[0],
                resource=key[1],
                pair=key[3],
                executions=streak,
                prouve=f"{proven:.0f}s",
                out_of_sync=latest["out_of_sync"],
                motif=verdict["blocked"],
            )
            verdicts.append(verdict)
            continue

        if CFG["dry_run"]:
            log(
                "WARN",
                "VERDICT detecte, MODE OBSERVATION : aurait renegocie la paire",
                defaut=defect,
                noeud=key[0],
                resource=key[1],
                pair=key[3],
                executions=streak,
                prouve=f"{proven:.0f}s",
                out_of_sync=latest["out_of_sync"],
            )
            verdicts.append(verdict)
            continue

        pod = next((p for p, n in pods if n == key[0]), None)

        tags = (
            f'node="{escape(key[0])}",resource="{escape(key[1])}",'
            f'peer="{escape(key[3])}",defect="{escape(defect)}"'
        )
        try:
            renegotiate(kube, pod, key[1], key[3])
            verdict["acted"] = True
            # La trace de la divergence vient d'être purgée : on programme sa reconstitution.
            # Le lancement effectif attend le balayage suivant, la connexion pouvant encore
            # être en `Connecting` — voir `relever_verifications`.
            if defect == "stale_bitmap" and CFG["verify_after_act"]:
                lancer_verification(kube, pod, state, key, now)
            acted_resources.add(key[1])
            state["actions"].append({"ts": now, "key": marker, "defect": defect})
            counter = f"actions_total|{tags}"
            state["counters"][counter] = state["counters"].get(counter, 0) + 1
            # La série repart de zéro : la paire doit se requalifier entièrement avant toute
            # nouvelle action, même si le déblocage a échoué silencieusement.
            fresh_pairs.pop(marker, None)
            log(
                "WARN",
                "ACTION renegociation effectuee",
                defaut=defect,
                noeud=key[0],
                resource=key[1],
                pair=key[3],
                out_of_sync=latest["out_of_sync"],
            )
        except (RuntimeError, OSError, ValueError) as exc:
            counter = f"action_failures_total|{tags}"
            state["counters"][counter] = state["counters"].get(counter, 0) + 1
            log(
                "ERROR",
                "ACTION en echec",
                resource=key[1],
                pair=key[3],
                erreur=str(exc)[:200],
            )
        verdicts.append(verdict)

    state["pairs"] = fresh_pairs

    # 🛑 HORS DE LA BOUCLE DES VERDICTS, ET C'EST INDISPENSABLE. Après une renégociation
    # réussie le bitmap est purgé, donc la paire ne produit plus aucun verdict : un relevé
    # branché sur les verdicts ne lirait jamais le résultat de la vérification qu'il a
    # lui-même demandée.
    divergences = relever_verifications(kube, pods, state, now)

    # Constat seul : une resource orpheline ne se retire pas d'un automate. `drbdsetup down`
    # sur une resource réellement utilisée couperait un volume vivant, et rien ici ne permet
    # de distinguer « oubliée de LINSTOR » de « en cours de création ».
    orphelines = relever_orphelines(kube, pods, resources) if CFG["detect_orphans"] else []

    stats = {"samples": len(samples), "nodes": len(pods), "pairs": len(keys)}
    push(build_metrics(verdicts, state, stats, divergences, orphelines))
    kube.write_state(state)
    log(
        "INFO",
        "balayage termine",
        paires=len(keys),
        suspectes=len(verdicts),
        duree=f"{time.time() - STARTED:.1f}s",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
