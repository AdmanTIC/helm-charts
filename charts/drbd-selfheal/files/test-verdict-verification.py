#!/usr/bin/env python3
"""Scénarios de référence pour la lecture du verdict d'une vérification en ligne.

🛑 CETTE LECTURE DÉCIDE SI LE BALAYEUR A LE DROIT DE PURGER UN BITMAP. Se tromper de sens sur
une seule de ces chaînes, c'est soit détruire une divergence réelle, soit ne plus jamais
débloquer un bitmap fantôme. Les chaînes ci-dessous sont RELEVÉES sur un cluster réel, sur
DRBD 9.3.3 — elles ne sont pas inventées.

Exécution : `python3 charts/drbd-selfheal/files/test-verdict-verification.py`
Aucune dépendance, aucun accès réseau : les échanges avec le noyau sont simulés.
"""
import os
import sys

os.environ.setdefault("DRY_RUN", "true")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "balayeur", os.path.join(os.path.dirname(os.path.abspath(__file__)), "drbd-selfheal.py")
)
balayeur = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(balayeur)


class FauxKube:
    """Rend un `dmesg` figé, et note les commandes reçues."""

    def __init__(self, sortie):
        self.sortie = sortie
        self.appels = []

    def exec_pod(self, pod, argv):
        self.appels.append(argv)
        if argv[0] == "sh":
            return self.sortie, ""
        return "", ""


RES = "pvc-c0169b60-7dfd-4737-b2fb-9b9551aa7538"

PROPRE = (
    f"[95148.605968] drbd {RES}/0 drbd1004 storage3: "
    "Online verify done (total 241 sec; paused 0 sec; 43516 K/sec)\n"
)
DIVERGENT = (
    f"[94306.190214] drbd {RES}/0 drbd1004 storage3: "
    "Online verify done (total 243 sec; paused 0 sec; 43156 K/sec)\n"
    f"[94306.190224] drbd {RES}/0 drbd1004 storage3: "
    "Online verify found 6 4k blocks out of sync!\n"
)
SKIPPED = (
    f"[94766.857357] drbd {RES}/0 drbd1004 storage2: "
    "Online verify done but 2 4k blocks skipped (total 232 sec; paused 0 sec; 45204 K/sec)\n"
)
EN_COURS = f"[95400.000000] drbd {RES}/0 drbd1004 storage3: Starting Online Verify from sector 0\n"

CAS = [
    (
        "vérification propre : aucune ligne « found », donc bitmap fantôme",
        PROPRE, "storage3", 0.0, ("propre", 0),
    ),
    (
        "6 blocs divergents : divergence RÉELLE, l'action doit être retenue",
        DIVERGENT, "storage3", 0.0, ("divergent", 6),
    ),
    (
        "blocs « skipped » : trop occupés pour être comparés, PAS des divergences",
        SKIPPED, "storage2", 0.0, ("propre", 0),
    ),
    (
        "vérification en cours : aucune conclusion, on attend",
        EN_COURS, "storage3", 0.0, (None, 0),
    ),
    (
        "verdict ANTÉRIEUR à la sonde : ignoré, sinon on conclurait sur du passé",
        DIVERGENT, "storage3", 99999.0, (None, 0),
    ),
    (
        "verdict propre postérieur à une divergence ancienne : seul le récent compte",
        DIVERGENT + PROPRE, "storage3", 94400.0, ("propre", 0),
    ),
    (
        "ligne d'un AUTRE pair : ne doit pas être attribuée à celui-ci",
        DIVERGENT, "storage2", 0.0, (None, 0),
    ),
    (
        "journal vide : aucune conclusion",
        "", "storage3", 0.0, (None, 0),
    ),
]



class FauxKubeConfigs:
    """Rend une liste de fichiers `.res`, ou leve si `panne` est vraie."""

    def __init__(self, res, panne=False):
        self.res = res
        self.panne = panne

    def exec_pod(self, pod, argv):
        if self.panne:
            raise RuntimeError("command terminated with non-zero exit code")
        return "\n".join(f"/var/lib/linstor.d/{r}.res" for r in self.res), ""


def tester_orphelines():
    """🛑 Le sens de ces scenarios est asymetrique, et c'est essentiel. Une resource au NOYAU
    sans configuration est une orpheline. L'inverse — declaree par LINSTOR, absente du noyau —
    n'en est PAS une : c'est une creation en cours, ou un nœud qui n'a pas encore adjuste.
    """
    reussis = 0
    PODS = [("pod/sat-worker3", "worker3")]

    cas = [
        (
            "noyau et configurations concordent : aucune orpheline",
            {("worker3", "pvc-aaa"), ("worker3", "pvc-bbb")},
            ["pvc-aaa", "pvc-bbb"], False, [],
        ),
        (
            "une resource au noyau sans configuration : ORPHELINE",
            {("worker3", "pvc-aaa"), ("worker3", "pvc-bbb")},
            ["pvc-aaa"], False, [("worker3", "pvc-bbb")],
        ),
        (
            "configuration declaree mais resource absente du noyau : PAS une orpheline",
            {("worker3", "pvc-aaa")},
            ["pvc-aaa", "pvc-bbb"], False, [],
        ),
        (
            "aucune configuration relevee : on ne conclut RIEN, jamais",
            {("worker3", "pvc-aaa"), ("worker3", "pvc-bbb")},
            [], False, [],
        ),
        (
            "releve en echec : on ne conclut RIEN, jamais",
            {("worker3", "pvc-aaa")},
            ["pvc-aaa"], True, [],
        ),
        (
            "un noeud sans aucune resource au noyau : rien a comparer",
            set(),
            [], False, [],
        ),
        (
            "les resources d'un AUTRE noeud ne comptent pas pour celui-ci",
            {("worker3", "pvc-aaa"), ("storage1", "pvc-zzz")},
            ["pvc-aaa"], False, [],
        ),
    ]

    for intitule, resources, res, panne, attendu in cas:
        kube = FauxKubeConfigs(res, panne)
        obtenu = [(o["node"], o["resource"])
                  for o in balayeur.relever_orphelines(kube, PODS, resources)]
        if obtenu == attendu:
            print(f"  OK   {intitule}")
            reussis += 1
        else:
            print(f"  ECHEC {intitule}\n        attendu {attendu}, obtenu {obtenu}")

    # La commande passee au shell ne doit porter aucune donnee variable.
    kube = FauxKubeConfigs(["pvc-aaa"])
    appels = []
    kube_orig = kube.exec_pod

    def espion(pod, argv):
        appels.append(argv)
        return kube_orig(pod, argv)

    kube.exec_pod = espion
    balayeur.relever_orphelines(kube, PODS, {("worker3", "pvc-aaa")})
    if appels and appels[0][0] == "sh" and "pvc-" not in " ".join(appels[0]):
        print("  OK   la commande de relevé ne porte aucune donnée variable")
        reussis += 1
    else:
        print(f"  ECHEC commande suspecte : {appels}")

    return reussis, len(cas) + 1


def principal():
    reussis = 0
    for intitule, journal, pair, depuis, attendu in CAS:
        kube = FauxKube(journal)
        obtenu = balayeur.verdict_verification(kube, "pod/x", RES, pair, depuis)
        if obtenu == attendu:
            print(f"  OK   {intitule}")
            reussis += 1
        else:
            print(f"  ECHEC {intitule}\n        attendu {attendu}, obtenu {obtenu}")

    # La chaîne confiée au shell doit rester CONSTANTE : aucune donnée ne doit y transiter.
    kube = FauxKube(PROPRE)
    balayeur.lignes_verification(kube, "pod/x", RES, "storage3")
    commande = kube.appels[0]
    if RES not in " ".join(commande) and commande[0] == "sh":
        print("  OK   la commande shell ne porte aucune donnée variable")
        reussis += 1
    else:
        print(f"  ECHEC la commande shell porte des données variables : {commande}")

    # 🛑 Les deux SENS d'une même connexion doivent partager UNE sonde. Sans cela le second
    # sens relance un `drbdadm verify` sur une connexion deja en verification, que DRBD refuse
    # avec le code 11. Defaut constate a l'essai.
    aller = balayeur.cle_sonde(("storage1", RES, "0", "storage3"))
    retour = balayeur.cle_sonde(("storage3", RES, "0", "storage1"))
    if aller == retour:
        print("  OK   les deux sens d'une connexion partagent une seule sonde")
        reussis += 1
    else:
        print(f"  ECHEC sondes distinctes : {aller} != {retour}")

    # Deux connexions differentes de la meme resource ne doivent PAS se confondre.
    autre = balayeur.cle_sonde(("storage1", RES, "0", "storage2"))
    if autre != aller:
        print("  OK   deux connexions distinctes gardent des sondes distinctes")
        reussis += 1
    else:
        print("  ECHEC deux connexions distinctes partagent une sonde")

    print("")
    gagnes, sur = tester_orphelines()
    reussis += gagnes

    total = len(CAS) + 3 + sur
    print(f"\n{reussis}/{total} scénarios conformes")
    return 0 if reussis == total else 1


if __name__ == "__main__":
    sys.exit(principal())
