## Why

re-agent sait aujourd’hui assister le reverse de fonctions et produire un arbre C++ partiellement compilable, mais ne transforme pas encore un binaire arbitraire en artefact reconstruisible et vérifiable. Le contrat ABI récemment ajouté est validé au chargement, sans contraindre le Transform ; l’assembleur ne lie pas un projet final et la parité dynamique reste abstraite. Cette situation rend un résultat « preserve_abi » trompeur et empêche BGE comme tout autre projet cible de disposer d’une chaîne de preuves exploitable.

Cette évolution doit faire de re-agent un cœur générique, piloté par des contrats et des recettes d’adaptateur, sans règles BGE, architecture ou toolchain intégrées au produit.

## What Changes

- Ajouter un cycle de vie de projet générique : identité binaire hashée, provisionnement, snapshot d’analyse exportable et répertoires de runs isolés.
- Remplacer les defaults cible-spécifiques par un profil neutre et une configuration explicite du backend, de la cible et de la toolchain.
- Rendre le Transform `preserve_abi` réellement contractuel avec un mode mono-cible, des prompts dédiés, une validation stricte des artefacts et une compilation obligatoire.
- Ajouter RunLock, provenance des artefacts, replay hors ligne et reprise déterministe par cible.
- Construire le bulk et Assemble au-dessus du chemin mono-cible, avec couverture complète du manifest, staging, publication atomique et recette externe de build/link fail-closed.
- Définir les contrats génériques de build et de vérification différentielle ; l’adaptateur cible fournit toolchain, inspection binaire, harness et observables runtime.
- Ajouter un registre de preuves et des transitions de promotion monotones : compilation, preuve ABI externe, différentiel, puis promotion.
- **BREAKING** : les configurations implicites (paths, macros, `-m32`, Ghidra CLI et conventions de compilation) cessent d’être des defaults du cœur ; elles doivent être déclarées par le profil/adaptateur.
- **BREAKING** : le pipeline `preserve_abi` refuse tout Transform bulk, Assemble ou publication non soutenu par les preuves disponibles, jusqu’à l’activation du jalon correspondant.

## Non-goals

- Ne pas implémenter de logique BGE, Jade, x87, proxy DLL ou recette de link spécifique dans re-agent.
- Ne pas créer un framework de plugins Python, une base de données, une UI web ou un scheduler distribué.
- Ne pas supporter IDA ou Binary Ninja dans ce changement ; Ghidra et les exports hors ligne définissent le premier contrat backend.
- Ne pas présenter une compilation, une regex ou une parity statique comme une preuve ABI ou comportementale.
- Ne pas viser l’identité binaire byte-à-byte comme condition de sortie initiale.

## Capabilities

### New Capabilities
- `project-provisioning`: identifie, fige et prépare un projet binaire et son snapshot d’analyse de manière générique.
- `target-toolchain-contracts`: décrit, vérifie et fingerprint le backend, la cible et la toolchain sans defaults spécifiques.
- `manifest-bound-transform`: génère et valide une transformation mono-cible strictement liée à une entrée ABI.
- `reproducible-run-evidence`: capture les entrées, appels LLM, artefacts et états nécessaires au replay et à la reprise fail-closed.
- `verified-project-build`: orchestre couverture, compilation, build/link externe, staging et publication atomique d’un artefact final.
- `differential-promotion`: lie les preuves ABI et comportementales aux transitions de promotion d’une cible ou d’un projet.

### Modified Capabilities
- Aucun. Aucun référentiel OpenSpec existant n’est présent dans `openspec/specs/`.

## Impact

Les changements concernent principalement `re-agent/src/re_agent/{cli,config,contracts,reverse,build,verify}`, les tests et la documentation re-agent. Les étapes affectées sont provision/analyse, reverse, Transform, Assemble/build, validation et parity. Le principal risque de parité est de confondre une source compilable avec une fonction ou un binaire équivalent : tout état incomplet ou toute preuve inconnue doit donc bloquer la publication et la promotion.