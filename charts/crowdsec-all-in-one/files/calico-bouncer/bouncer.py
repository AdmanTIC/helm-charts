#!/usr/bin/env python3
"""
crowdsec-calico-bouncer
=======================

Consumes the CrowdSec LAPI decisions *stream* endpoint and reconciles the
banned IPs into a Calico GlobalNetworkPolicy targeting HostEndpoints.

Behaviour:

  * Uses the *delta* stream API (``/v1/decisions/stream``) — fetches the full
    snapshot once (``startup=true``), then only the changes (new / deleted).
  * Tracks decisions by their LAPI id (NOT by CIDR), so multiple decisions
    targeting the same IP (cscli + crowdsec, or agent replacements on
    recurring alerts) coexist correctly — the IP is removed from the GNP
    only when the **last** decision for it is gone.
  * Maintains an in-memory mirror of the GNP nets; the resource is only
    re-applied when the set actually changes.
  * Handles deletions (the LAPI ``deleted`` array) — expired or manually
    removed bans are dropped from the policy when no other decision pins
    them.
  * Leader election via ``coordination.k8s.io/Lease`` — multiple replicas can
    run for HA but only one writes the GNP at any given time.
  * Exponential backoff on transient errors (network, k8s API), with jitter.
  * The GNP spec (selector, preDNAT, applyOnForward, doNotTrack, order, types)
    is fully driven by environment variables so the same image can be tuned
    for both iptables and eBPF dataplane configurations.

Required env:

  LAPI_URL                   e.g. http://crowdsec-service:8080
  LAPI_API_KEY               bouncer API key
  NAMESPACE                  K8s namespace where the Lease lives

Optional env (defaults shown):

  ACT_ON_TYPES=ban           comma-separated decision types to enforce as
                              network blocks. Add `captcha` if you don't run
                              an L7 bouncer (nginx/traefik) and want L3 hard-
                              block for captcha decisions too.
  LAPI_VERIFY_TLS=true
  BAN_APPLY_DELAY_SECONDS=0     grace delay before a new ban lands in the GNP,
                              so an L7 bouncer can serve a clean 403/captcha
                              first (resolution = POLL_INTERVAL_SECONDS)
  POLL_INTERVAL_SECONDS=10
  BACKOFF_MAX_SECONDS=300
  LEASE_NAME=crowdsec-calico-bouncer
  LEASE_DURATION_SECONDS=30
  LEASE_RENEW_INTERVAL=10
  IDENTITY=<hostname>
  GNP_NAME=crowdsec-bans
  GNP_SELECTOR="crowdsec-protected == 'true'"
  GNP_TYPES=Ingress              comma-separated, Ingress/Egress
  GNP_PRE_DNAT=true
  GNP_APPLY_ON_FORWARD=true
  GNP_DO_NOT_TRACK=false
  GNP_ORDER=100
  LOG_LEVEL=INFO
"""

from __future__ import annotations

import logging
import os
import random
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("calico-bouncer")

LAPI_URL = os.environ["LAPI_URL"].rstrip("/")
LAPI_API_KEY = os.environ["LAPI_API_KEY"]
LAPI_VERIFY_TLS = os.getenv("LAPI_VERIFY_TLS", "true").lower() == "true"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
BACKOFF_MAX = int(os.getenv("BACKOFF_MAX_SECONDS", "300"))

# Grace delay before a NEW ban is materialised in the GNP. Gives an L7
# bouncer (nginx/traefik) time to serve a clean 403/captcha to a legitimate
# user before the network goes dark for them. 0 = immediate. Resolution is
# bounded by POLL_INTERVAL. Bans already active when the bouncer takes over
# (initial snapshot after leader acquisition) are applied immediately.
BAN_APPLY_DELAY = float(os.getenv("BAN_APPLY_DELAY_SECONDS", "0"))

# Decision types that we materialise as a Calico Deny rule. Calico is L3/L4,
# so "captcha" / "throttle" cannot be honoured as such — including them here
# means "treat as hard block at the network layer".
ACT_ON_TYPES = {
    t.strip().lower()
    for t in os.getenv("ACT_ON_TYPES", "ban").split(",")
    if t.strip()
}

NAMESPACE = os.environ["NAMESPACE"]
LEASE_NAME = os.getenv("LEASE_NAME", "crowdsec-calico-bouncer")
LEASE_DURATION_SECONDS = int(os.getenv("LEASE_DURATION_SECONDS", "30"))
LEASE_RENEW_INTERVAL = int(os.getenv("LEASE_RENEW_INTERVAL", "10"))
IDENTITY = os.getenv("IDENTITY") or socket.gethostname()

GNP_NAME = os.getenv("GNP_NAME", "crowdsec-bans")
GNP_SELECTOR = os.getenv("GNP_SELECTOR", "crowdsec-protected == 'true'")
GNP_TYPES = [s.strip() for s in os.getenv("GNP_TYPES", "Ingress").split(",") if s.strip()]
GNP_PRE_DNAT = os.getenv("GNP_PRE_DNAT", "true").lower() == "true"
GNP_APPLY_ON_FORWARD = os.getenv("GNP_APPLY_ON_FORWARD", "true").lower() == "true"
GNP_DO_NOT_TRACK = os.getenv("GNP_DO_NOT_TRACK", "false").lower() == "true"
GNP_ORDER = float(os.getenv("GNP_ORDER", "100"))

CALICO_GROUP = "crd.projectcalico.org"
CALICO_VERSION = "v1"
CALICO_GNP_PLURAL = "globalnetworkpolicies"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_k8s() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def normalize_cidr(scope: str, value: str) -> str | None:
    """Return a CIDR string Calico understands, or None to skip."""
    if not value:
        return None
    scope = (scope or "").lower()
    if scope == "ip":
        if "/" in value:
            return value
        return f"{value}/128" if ":" in value else f"{value}/32"
    if scope == "range":
        return value if "/" in value else None
    return None


# ---------------------------------------------------------------------------
# Leader election (Lease-based)
# ---------------------------------------------------------------------------

class LeaderElection:
    """Minimal Lease-based leader election.

    A background thread periodically:
      * acquires the Lease (creates it if missing, or steals an expired one);
      * renews its hold while it is the leader;
      * relinquishes leadership if the API rejects a renew (e.g. lost race).
    """

    def __init__(
        self,
        coord_api: client.CoordinationV1Api,
        name: str,
        namespace: str,
        identity: str,
        lease_duration_seconds: int = 30,
        renew_interval: int = 10,
    ) -> None:
        self.coord_api = coord_api
        self.name = name
        self.namespace = namespace
        self.identity = identity
        self.lease_duration_seconds = lease_duration_seconds
        self.renew_interval = renew_interval
        self._stop = threading.Event()
        self._leader = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="leader")

    @property
    def is_leader(self) -> bool:
        with self._lock:
            return self._leader

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _set_leader(self, val: bool) -> None:
        with self._lock:
            if self._leader != val:
                log.info("leadership: %s", "ACQUIRED" if val else "LOST")
            self._leader = val

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._try_acquire_or_renew()
            except Exception:
                log.exception("leader election error")
                self._set_leader(False)
            self._stop.wait(self.renew_interval)

    def _try_acquire_or_renew(self) -> None:
        try:
            lease = self.coord_api.read_namespaced_lease(self.name, self.namespace)
        except ApiException as e:
            if e.status != 404:
                raise
            lease = None

        now = datetime.now(timezone.utc)

        if lease is None:
            self.coord_api.create_namespaced_lease(
                namespace=self.namespace,
                body=client.V1Lease(
                    metadata=client.V1ObjectMeta(name=self.name, namespace=self.namespace),
                    spec=client.V1LeaseSpec(
                        holder_identity=self.identity,
                        lease_duration_seconds=self.lease_duration_seconds,
                        acquire_time=now,
                        renew_time=now,
                    ),
                ),
            )
            self._set_leader(True)
            return

        spec = lease.spec
        is_held_by_us = spec and spec.holder_identity == self.identity
        is_expired = False
        if spec and spec.renew_time:
            rt = spec.renew_time
            if rt.tzinfo is None:
                rt = rt.replace(tzinfo=timezone.utc)
            ttl = spec.lease_duration_seconds or self.lease_duration_seconds
            is_expired = now >= rt + timedelta(seconds=ttl)

        if is_held_by_us:
            lease.spec.renew_time = now
            try:
                self.coord_api.replace_namespaced_lease(self.name, self.namespace, lease)
                self._set_leader(True)
            except ApiException as e:
                if e.status == 409:
                    log.warning("lease renew conflict — dropping leadership")
                    self._set_leader(False)
                else:
                    raise
            return

        if is_expired:
            lease.spec.holder_identity = self.identity
            lease.spec.acquire_time = now
            lease.spec.renew_time = now
            lease.spec.lease_duration_seconds = self.lease_duration_seconds
            try:
                self.coord_api.replace_namespaced_lease(self.name, self.namespace, lease)
                self._set_leader(True)
            except ApiException as e:
                if e.status == 409:
                    # Someone else won the race.
                    self._set_leader(False)
                else:
                    raise
            return

        self._set_leader(False)


# ---------------------------------------------------------------------------
# GlobalNetworkPolicy manager
# ---------------------------------------------------------------------------

class GnpManager:
    def __init__(self, custom_api: client.CustomObjectsApi) -> None:
        self.api = custom_api
        # None = "no known state, force the next write". An empty set means
        # "we know the GNP holds zero nets" and the next ensure(set()) can
        # safely no-op. The distinction matters at startup / after a leader
        # takeover: we must reconcile even if the LAPI tells us the active
        # set is empty (e.g. long downtime where everything expired) — the
        # cluster GNP may still hold stale entries.
        self._known_nets: set[str] | None = None

    @property
    def known_nets(self) -> set[str] | None:
        return self._known_nets

    def _spec(self, nets_sorted: list[str]) -> dict:
        ingress_rules = []
        if nets_sorted:
            ingress_rules.append(
                {
                    "action": "Deny",
                    "source": {"nets": nets_sorted},
                }
            )
        spec: dict = {
            "selector": GNP_SELECTOR,
            "order": GNP_ORDER,
            "types": GNP_TYPES,
            "ingress": ingress_rules,
        }
        if GNP_PRE_DNAT:
            spec["preDNAT"] = True
        if GNP_APPLY_ON_FORWARD:
            spec["applyOnForward"] = True
        if GNP_DO_NOT_TRACK:
            spec["doNotTrack"] = True
        return spec

    def ensure(self, nets: set[str]) -> None:
        if nets == self._known_nets:
            log.debug("GNP unchanged (%d nets)", len(nets))
            return

        nets_sorted = sorted(nets)
        body = {
            "apiVersion": f"{CALICO_GROUP}/{CALICO_VERSION}",
            "kind": "GlobalNetworkPolicy",
            "metadata": {
                "name": GNP_NAME,
                "labels": {
                    "app.kubernetes.io/managed-by": "crowdsec-calico-bouncer",
                },
            },
            "spec": self._spec(nets_sorted),
        }

        try:
            existing = self.api.get_cluster_custom_object(
                CALICO_GROUP, CALICO_VERSION, CALICO_GNP_PLURAL, GNP_NAME
            )
            # Carry over resourceVersion so the update is consistent.
            body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
            self.api.replace_cluster_custom_object(
                CALICO_GROUP, CALICO_VERSION, CALICO_GNP_PLURAL, GNP_NAME, body
            )
            log.info("GNP %s updated (%d nets)", GNP_NAME, len(nets))
        except ApiException as e:
            if e.status == 404:
                self.api.create_cluster_custom_object(
                    CALICO_GROUP, CALICO_VERSION, CALICO_GNP_PLURAL, body
                )
                log.info("GNP %s created (%d nets)", GNP_NAME, len(nets))
            else:
                raise

        self._known_nets = set(nets)

    def reset_cache(self) -> None:
        self._known_nets = None


# ---------------------------------------------------------------------------
# Main stream consumer
# ---------------------------------------------------------------------------

def stream_loop(
    gnp: GnpManager,
    leader: LeaderElection,
    session: requests.Session,
) -> None:
    # Track decisions by their LAPI id, not by CIDR. Two decisions for the same
    # IP (cscli + crowdsec, or replacement of an old by a new) coexist in this
    # dict — the IP stays in the GNP as long as ANY of them is active.
    #
    # The bug we are fixing: with a `set[CIDR]` model, the LAPI's
    # "delete old / insert new" pattern emitted by the agent on duplicate
    # scenario hits (same IP) causes the CIDR to be both in `deleted` and
    # `new` in the same tick. `state |= new ; state -= del` then loses the
    # IP. Tracking by id avoids this: we add the new id BEFORE removing the
    # old id, so the union of values always contains the IP.
    decisions: dict[int, str] = {}
    # Per-CIDR earliest apply time (epoch seconds). Keyed by CIDR, not by
    # decision id, so an id swap on the same IP (delete old / insert new)
    # never re-arms the grace delay — the IP stays continuously banned.
    apply_at: dict[str, float] = {}
    startup = True
    initial_sync = True
    backoff = 1.0
    was_leader = False

    while True:
        if not leader.is_leader:
            if was_leader:
                log.info("no longer leader, idling")
                was_leader = False
            time.sleep(2)
            continue

        if not was_leader:
            log.info("became leader — refetching full snapshot")
            decisions = {}
            apply_at = {}
            startup = True
            initial_sync = True
            gnp.reset_cache()
            was_leader = True

        try:
            params = {
                "startup": "true" if startup else "false",
                "scopes": "ip,range",
            }
            r = session.get(
                f"{LAPI_URL}/v1/decisions/stream",
                params=params,
                headers={"X-Api-Key": LAPI_API_KEY},
                timeout=30,
                verify=LAPI_VERIFY_TLS,
            )
            r.raise_for_status()
            data = r.json() or {}
            new_decisions = data.get("new") or []
            del_decisions = data.get("deleted") or []

            # On startup, the LAPI returns the full active set — wipe the
            # local cache to avoid keeping stale ids that the LAPI no longer
            # knows about (e.g. items expired while we were not running).
            if startup:
                decisions = {}

            # Process deletions FIRST, then additions. This way, when the LAPI
            # emits an id swap for the same IP (delete old, insert new), the
            # final dict contains the new id pointing to that CIDR — the IP
            # is preserved.
            #
            # Deletions match by id AND by CIDR: the LAPI deduplicates the
            # stream per value, so a "delete all decisions for IP X" (several
            # decision ids) can surface as a single deleted entry whose id we
            # may not even hold. Dropping every id that maps to the deleted
            # CIDR guarantees the IP leaves the GNP; a same-tick re-insert
            # (id swap) is processed after and puts it back.
            removed = 0
            for d in del_decisions:
                did = d.get("id")
                if did is not None and decisions.pop(did, None) is not None:
                    removed += 1
                cidr = normalize_cidr(d.get("scope", ""), d.get("value", ""))
                if cidr:
                    stale = [i for i, c in decisions.items() if c == cidr]
                    for i in stale:
                        del decisions[i]
                    removed += len(stale)

            added = 0
            for d in new_decisions:
                if (d.get("type") or "").lower() not in ACT_ON_TYPES:
                    continue
                did = d.get("id")
                if did is None:
                    continue
                cidr = normalize_cidr(d.get("scope", ""), d.get("value", ""))
                if not cidr:
                    continue
                if did not in decisions:
                    added += 1
                decisions[did] = cidr

            now_ts = time.time()
            cidrs = set(decisions.values())

            # Schedule newly-seen CIDRs; forget CIDRs no decision pins anymore.
            # The initial snapshot after leader acquisition applies immediately
            # (those bans are already old); only fresh deltas get the grace
            # delay.
            for c in cidrs:
                if c not in apply_at:
                    apply_at[c] = now_ts if initial_sync else now_ts + BAN_APPLY_DELAY
            for c in list(apply_at):
                if c not in cidrs:
                    del apply_at[c]

            nets = {c for c in cidrs if apply_at[c] <= now_ts}
            pending = len(cidrs) - len(nets)

            if nets != gnp.known_nets:
                log.info(
                    "delta: +%d -%d decisions → %d active, %d nets enforced, %d pending delay",
                    added,
                    removed,
                    len(decisions),
                    len(nets),
                    pending,
                )
                gnp.ensure(nets)
            else:
                log.debug(
                    "no GNP change (%d active decisions, %d nets enforced, %d pending delay)",
                    len(decisions),
                    len(nets),
                    pending,
                )

            startup = False
            initial_sync = False
            backoff = 1.0
            time.sleep(POLL_INTERVAL)

        except (requests.RequestException, ApiException, ValueError) as e:
            log.warning("transient error: %s — backing off %.1fs", e, backoff)
            time.sleep(backoff + random.uniform(0, 1))
            backoff = min(backoff * 2, BACKOFF_MAX)
            # On error, force the next tick to refetch the full snapshot
            # to avoid drifting from the LAPI state.
            startup = True
        except Exception:
            log.exception("unexpected error")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            startup = True


def main() -> None:
    load_k8s()
    coord_api = client.CoordinationV1Api()
    custom_api = client.CustomObjectsApi()

    leader = LeaderElection(
        coord_api=coord_api,
        name=LEASE_NAME,
        namespace=NAMESPACE,
        identity=IDENTITY,
        lease_duration_seconds=LEASE_DURATION_SECONDS,
        renew_interval=LEASE_RENEW_INTERVAL,
    )
    leader.start()

    gnp = GnpManager(custom_api)
    session = requests.Session()

    log.info(
        "starting bouncer LAPI=%s GNP=%s selector=%r preDNAT=%s applyOnForward=%s actOn=%s identity=%s",
        LAPI_URL,
        GNP_NAME,
        GNP_SELECTOR,
        GNP_PRE_DNAT,
        GNP_APPLY_ON_FORWARD,
        sorted(ACT_ON_TYPES),
        IDENTITY,
    )
    try:
        stream_loop(gnp, leader, session)
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    finally:
        leader.stop()


if __name__ == "__main__":
    main()
