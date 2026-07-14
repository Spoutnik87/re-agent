# Design — make-re-agent-project-recompilation-ready

## 1. Contexte

re-agent sait aujourd'hui inverser des fonctions et produire un arbre C++ partiellement compilable, mais il ne transforme pas un binaire arbitraire en artefact recompilable et vérifiable de bout en bout. Le contrat ABI récemment ajouté est validé au chargement sans contraindre le Transform ; l'assembleur ne lie pas un projet final ; la parité dynamique reste abstraite. Cette situation rend un résultat « preserve_abi » trompeur et empêche tout projet cible (BGE ou autre) de disposer d'une chaîne de preuves exploitable.

Le changement fait de re-agent un cœur générique : il orchestre le cycle de vie projet, l'exécution des transformations et la collecte de preuves, mais **n'affirme jamais lui-même la conformité ABI ou comportementale**. Ces preuves sont produites et vérifiées par des adaptateurs externes. Le cœur émet uniquement des verdicts intermédiaires reproductibles (`MANIFEST_BOUND`, `COMPILE_PASS`) sur lesquels les adaptateurs peuvent construire.

Tout ce qui est aujourd'hui implicite (paths, macros, `-m32`, Ghidra CLI) doit être déclaré dans un profil ou une recette externe. Les données utilisateur (profils YAML, recettes, adaptateurs) peuvent mentionner « BGE » librement ; seule l'intégration de règles BGE en dur dans le code du cœur est interdite.

## 2. Buts et non-buts

### Buts (P0)

| But | Critère de succès |
|---|---|
| **ProjectManifest** : identité binaire hachée, provisionnement, répertoires isolés. | `re-agent project init <binary>` produit `manifest.yaml` ; `re-agent project status` montre l'état. |
| **Snapshot séparé** : copie figée et horodatée des exports Ghidra (ou autre backend) au moment du provisionnement. | `snapshot/` contient ABI, structs, xrefs ; le manifest enregistre le hash du snapshot. |
| **Target/Toolchain contracts** : backend, cible et toolchain décrits dans un profil YAML fourni par l'utilisateur ou l'adaptateur. | Le cœur re-agent ne contient plus `-m32`, `g++`, `GhidraHeadless` en dur. |
| **Manifest-bound transform** : transform mono-cible lié à une entrée ABI du manifest, via la CLI existante `re-agent build --phase transform --address <addr>`. | `--phase transform --address <addr>` lit l'ABI, génère le code, compile. Émet `MANIFEST_BOUND` + `COMPILE_PASS`. |
| **RunLock OS + metadata** : verrou OS (flock / LockFile) + metadata (hostname, pid, timestamp, command line). | Crash → relance détectée ; stale lock > 30 min nettoyable. |
| **Replay déterministe complet** : toutes les entrées/sorties LLM capturées, reproductible sans LLM. | `re-agent replay <run-id>` rejoue exactement les mêmes transformations. |
| **Build recipe externe** : compilation + link délégués à un script fourni par l'adaptateur. | `re-agent build --phase link` invoque `{project}/recipe/build.sh` avec le staging. |
| **Evidence append-only dérivée** : les preuves ne sont jamais modifiées, seulement ajoutées ; chaque preuve dérive des entrées du run. | `{run}/evidence/` contient les fichiers, `evidence_index.json` est append-only. |
| **Rôles core / adaptateur clairs** : le cœur orchestre, l'adaptateur fournit la connaissance cible. | Aucun fichier `.py` du cœur ne contient de chaîne `BGE`, `jade`, `x87`, `d3d9`. |

### Non-buts

- Le cœur n'implémente pas de vérification ABI, de preuve comportementale, de diff binaire ou de promotion.
- Ne pas créer un framework de plugins Python, une base de données, une UI web ou un scheduler distribué.
- Ne pas supporter IDA ou Binary Ninja dans ce changement ; Ghidra (headless ou bridge) et les exports hors ligne (JSON) définissent le premier contrat backend. L'adaptateur peut en ajouter d'autres.
- Ne pas viser l'identité binaire byte-à-byte comme condition de sortie.
- Ne pas interdire le mot « BGE » dans les données utilisateur (profils, recettes, adaptateurs). Seules les règles codées en dur dans le cœur sont interdites.

## 3. Responsabilités core / adaptateur

| Domaine | Cœur (re-agent) | Adaptateur (projet cible) |
|---|---|---|
| Cycle de vie projet | Init, snapshot, runs, lock, orchestration | Définit la configuration cible |
| Transform | Lit l'ABI du manifest, génère le code avec le LLM, compile | Fournit les prompts additionnels, les contraintes de type |
| Build | Compile chaque source, copie les `.o` dans staging, invoque la recette | Fournit la recette de link, les dépendances, les flags |
| Preuve ABI | **N'affirme pas** — émet `MANIFEST_BOUND` (la source correspond à l'entrée ABI) + `COMPILE_PASS` (la source compile) | Vérifie et affirme `ABI_PROVEN` (fingerprint des symboles match) |
| Preuve runtime | **N'affirme pas** — ne produit que des artefacts reproductibles | Exécute le binaire, compare les traces, affirme `BEHAVIOR_MATCH` |
| Promotion | Fournit le squelette (états, transitions), n'autorise pas de promotion par lui-même | Utilise les preuves pour décider de la promotion |

## 4. Architecture

### 4.1 ProjectManifest

Le projet est un répertoire contenant :

```
{project}/
  manifest.yaml          # Identité, hash binaire, empreinte snapshot, adaptateur
  target.yaml            # Profil cible : arch, os, calling_convention, toolchain, backend
  snapshot/              # Copie figée des exports backend (ABI, structs, xrefs, asm)
    abi_index.json       # Index des symboles exportés avec signatures
    structs.json         # Définitions de types
    xrefs.json           # Références croisées
    <backend>_meta.json  # Version, date du snapshot
  runs/                  # Répertoires de runs isolés (run_<timestamp>_<uuid>/)
  recipe/                # Recettes de build fournies par l'adaptateur
    compile.sh           # Script de compilation (optionnel si le cœur compile direct)
    build.sh             # Script de link / production de l'artefact final
  adapters/              # Runners personnalisés (optionnel)
    abi_prove.sh
    diff.sh
```

`manifest.yaml` :

```yaml
project:
  name: "bge"
  binary_sha256: "abcd1234..."
  binary_path: "/path/to/BGE.exe"
  snapshot_hash: "ef5678..."
  snapshot_date: "2026-07-14T12:00:00Z"
  adapters:
    prove_abi: "adapters/abi_prove.sh"
    diff: "adapters/diff.sh"
```

### 4.2 Target/Toolchain profile

`target.yaml` :

```yaml
backend:
  type: ghidra_headless          # ghidra_headless | ghidra_bridge | offline
  ghidra_dir: "C:/ghidra_11.3"
  headless_script: "analyze.py"

target:
  arch: "i386"
  os: "windows"
  calling_convention: "cdecl"

toolchain:
  capabilities:
    compilation: true
    linking: false               # Si false, la recette doit fournir le link
    static_analysis: false
  compiler: "g++"
  compiler_flags: ["-std=c++23", "-m32", "-Wall", "-Werror"]
  linker: "g++"
  link_flags: ["-m32", "-static"]
  recipe: "recipe/build.sh"
```

Les **capabilities toolchain** permettent au cœur de savoir ce qu'il peut faire directement et ce qui nécessite l'adaptateur. `compilation: true` = le cœur peut compiler les `.cpp` → `.o`. `linking: false` = le link est délégué à la recette.

### 4.3 Manifest-bound mono-target transform

Le chemin critique est la transformation d'une seule fonction via la CLI existante :

```
re-agent build --phase transform --address 0x00401234
```

Ce que fait le cœur :

1. Charge `manifest.yaml` + `target.yaml`
2. Trouve l'entrée ABI pour `<addr>` dans `snapshot/abi_index.json`
3. Exécute le Transform LLM avec l'ABI comme contrat d'entrée
4. Compile la source générée
5. Émet deux verdicts dans `{run}/verdicts/` :
   - `MANIFEST_BOUND` : la source est liée à une entrée ABI du manifest
   - `COMPILE_PASS` : la source compile sans erreur
6. Enregistre les artefacts dans `{run}/artifacts/` : source `.cpp`, objet `.o`, log compile

Le cœur **n'émet pas** de verdict `ABI_PROVEN`. C'est la responsabilité de l'adaptateur (via `adapters/abi_prove.sh`) de vérifier que le fingerprint ABI de la source correspond à l'original et d'émettre ce verdict.

### 4.4 RunLock OS + metadata

Implémentation :

- Lock au niveau du répertoire `{run}/` via `fcntl.flock` (POSIX) ou `LockFileEx` (Windows).
- Metadata écrites dans `{run}/.lock` : hostname, pid, timestamp, commande invoquée, `re-agent` version.
- Stale lock : si le timestamp > 30 minutes et le pid n'existe plus, le lock est considéré comme stale et peut être libéré.
- `re-agent run release` : libération manuelle.

### 4.5 Replay complet

Chaque run capture :

- `{run}/llm/` : chaque appel LLM (prompt → réponse), indexé par un hash du prompt.
- `{run}/inputs/` : copie de l'entrée ABI consommée, du manifest au moment du run.
- `{run}/artifacts/` : sources, objets, logs.
- `{run}/verdicts/` : verdicts émis (MANIFEST_BOUND, COMPILE_PASS, etc.).

`re-agent replay <run-id>` :

1. Vérifie que le manifest et le snapshot n'ont pas changé (hash).
2. Sert les réponses LLM depuis `{run}/llm/` (pas d'appel LLM réel).
3. Re-exécute la compilation.
4. Vérifie que les verdicts sont identiques.

### 4.6 Build recipe externe

`re-agent build --phase transform` : compile source → `.o` (via le compilateur déclaré dans `target.yaml`).

`re-agent build --phase link` : invoque `{project}/recipe/build.sh` avec les arguments suivants :

```
build.sh <staging_dir> <output_path> <manifest_of_objects.json>
```

La recette est responsable de produire l'artefact final (PE, ELF). Si la recette n'existe pas et que `toolchain.capabilities.linking` est `false`, le build échoue avec un message clair.

`re-agent build --verify-recipe` : invoque la recette avec un fichier objet témoin pour valider qu'elle produit un artefact.

### 4.7 Evidence append-only dérivée

Principes :

- `{run}/evidence/` : chaque preuve est un fichier horodaté, jamais modifié après écriture.
- `{run}/evidence_index.json` : append-only, chaque entrée contient `{timestamp, type, target, hash, file_path}`.
- Les preuves sont **dérivées** : elles sont produites par des étapes du run (transform, compile, link) ou par des adaptateurs externes. Rien n'est écrit manuellement.
- Types de preuves émises par le cœur :
  - `manifest_bound/<addr>.json` : preuve que la source est liée à l'entrée ABI
  - `compile/<addr>.log` : log de compilation
  - `compile/<addr>.o` : hash de l'objet compilé
  - `verdict/<addr>.json` : verdicts émis

Ne sont **pas** des preuves émises par le cœur :
  - `abi_proven/<addr>.fingerprint` : produit par l'adaptateur
  - `behavior_match/<addr>.trace` : produit par l'adaptateur

## 5. Décisions et alternatives

### D-1 : ProjectManifest en YAML, snapshot séparé

_Constat_ : le projet a besoin d'une identité stable et d'un état figé des exports backend.

_Décision_ : `manifest.yaml` pour l'identité et les métadonnées ; `snapshot/` pour les données d'analyse. Le snapshot est copié au moment du provisionnement, pas référencé en place. Cela permet de git-versionner le projet sans dépendre du binaire ou de Ghidra.

_Alternative écartée_ : stocker le snapshot dans le manifest (trop volumineux).

### D-2 : CLI existante `--phase transform --address` comme premier vecteur

_Constat_ : `re-agent build` a déjà une option `--phase`. L'étendre est plus rapide et moins cassant que de créer une nouvelle commande.

_Décision_ : la première PR ajoute `--phase transform --address <addr>` à la commande `build` existante. Les phases futures (`link`, `verify`, `promote`) suivront le même pattern.

_Alternative écartée_ : nouvelle commande `re-agent transform` (trop de changements pour une première PR).

### D-3 : Core émet MANIFEST_BOUND + COMPILE_PASS, pas ABI_PROVEN

_Constat_ : le cœur ne peut pas prouver que le code généré est équivalent à l'original — seule une exécution ou une analyse spécialisée peut le faire.

_Décision_ : le cœur émet des verdicts vérifiables et reproductibles. Les preuves fortes (ABI, comportement) sont produites par des adaptateurs externes, qui peuvent utiliser ou non les verdicts du cœur.

_Alternative écartée_ : le cœur tente de prouver l'ABI (faisabilité douteuse, responsabilité floue).

### D-4 : Aucune interdiction lexicale de « BGE » dans les données utilisateur

_Constat_ : un profil YAML ou une recette de build pour BGE doit pouvoir mentionner « BGE », « jade.exe », « d3d9.dll ». Ce sont des données, pas des règles codées.

_Décision_ : l'interdiction porte uniquement sur les chaînes BGE, `-m32`, `GhidraHeadless` dans le code source Python du cœur. Les profils, les recettes et les adaptateurs peuvent contenir ces termes librement.

### D-5 : RunLock OS (flock/LockFileEx) + metadata, pas de base de données

_Constat_ : le lock doit être fiable, atomique et ne pas nécessiter de processus externe.

_Décision_ : lock fichier via les appels OS natifs (flock sur Linux/Mac, LockFileEx sur Windows). Metadata dans le même fichier. Pas de DB, pas de service.

_Alternative écartée_ : fichier PID seul (pas atomique), Redis (trop lourd).

### D-6 : Build recipe externe obligatoire pour le link

_Constat_ : les flags de link, l'ordre des sections, les dépendances sont spécifiques à chaque cible.

_Décision_ : le cœur compile les sources en `.o` mais ne link jamais. Le link est toujours délégué à une recette externe. Si `toolchain.capabilities.linking` est `false` et la recette absente, le build échoue.

_Alternative écartée_ : linker générique dans le cœur (trop de variabilité, impossible à maintenir).

### D-7 : Evidence append-only, jamais modifiée

_Constat_ : l'intégrité des preuves est fondamentale pour la confiance dans le pipeline.

_Décision_ : les fichiers de preuve sont écrits une seule fois (create, never update). L'index est append-only. Toute tentative d'écraser une preuve existante est une erreur.

## 6. Risques

| Risque | Impact | Mitigation |
|---|---|---|
| R-1 : Profil YAML trop rigide pour certains projets | Moyen | Prévoir un champ `custom: {}` libre dans `target.yaml`, extensible par l'adaptateur. |
| R-2 : Replay cassé par un appel LLM aux mêmes entrées mais réponses différentes | Élevé | Le replay sert les réponses précédentes basées sur le hash du prompt. Si le hash n'est pas trouvé, le replay échoue plutôt que d'appeler le LLM. |
| R-3 : RunLock stale sur crash sans cleanup | Faible | Timeout de 30 min + `re-agent run release` manuel. Vérification de l'existence du pid au démarrage. |
| R-4 : Build recipe silencieusement incompatible | Élevé | `--verify-recipe` exécute la recette en dry-run et valide la production d'un artefact. |
| R-5 : Changement de version Ghidra entre snapshot et re-analyse | Faible | Le snapshot enregistre la version Ghidra ; `project analyze` avertit si différent. |
| R-6 : La séparation core/adaptateur est mal comprise par les développeurs | Moyen | Documenter clairement les responsabilités (section 3), ajouter des tests qui échouent si le cœur émet un verdict ABI. |

## 7. Plan par releases

Chaque release est une PR indépendante, publiable et testable.

### Release 1 — Manifest-bound mono-target (première PR)

**Scope** : ajouter `--phase transform --address <addr>` à la CLI `re-agent build` existante, avec chargement du manifest, de l'ABI, transform LLM, compilation, verdicts `MANIFEST_BOUND` + `COMPILE_PASS`, et enregistrement dans le run.

**Ce qui change dans le cœur** :

- `cli/build.py` : ajouter le parsing de `--phase transform` et `--address`.
- `contracts/project_manifest.py` : nouveau module (chargement, validation).
- `contracts/target_profile.py` : nouveau module (chargement, validation).
- `transform/` : refactor pour accepter une entrée ABI du manifest.
- `build/compile.py` : extraire la compilation en module partagé.
- `run/` : nouveau module (RunLock, run directory, LLM capture, verdicts, evidence).

**Ce qui change dans la config** :

- `re-agent.yaml` (ou équivalent) : ajouter les champs `project:`, `target:`, `backend:`.

**Validation** :

- `re-agent build --phase transform --address 0x00401234` sur BGE :
  - produit `MANIFEST_BOUND` + `COMPILE_PASS` dans `{run}/verdicts/`
  - produit la source `.cpp` et l'objet `.o`
  - produit les entrées LLM dans `{run}/llm/`
- `re-agent build --phase transform --address 0x00401234` sur un binaire factice (Linux ELF simple) avec un profil adapté fonctionne aussi.

**Non livré** : bulk, link, verify, promote, adaptateurs.

### Release 2 — RunLock, replay, snapshot propre

**Scope** : verrouillage OS des runs, capture complète LLM + inputs, `re-agent replay`, séparation nette du snapshot dans `snapshot/`.

### Release 3 — Bulk transform

**Scope** : `--phase transform --bulk --address-list <file>`, progression, reprise.

### Release 4 — Build recipe externe + link

**Scope** : `--phase link`, `--verify-recipe`, staging, signature de la recette.

### Release 5 — Adaptateurs, evidence append-only, pipelines externes

**Scope** : runners externalisables, evidence registry, chaîne de promotion squelettique (le cœur fournit les états, l'adaptateur fournit les transitions).

## 8. Questions ouvertes

1. **Q-1** : Le snapshot doit-il inclure uniquement l'analyse Ghidra (ABI, structs, xrefs) ou aussi une copie du `.text` original pour la vérification ABI ? **Suggestion** : seulement l'analyse. L'adaptateur peut ajouter une copie du `.text` s'il en a besoin.

2. **Q-2** : Gestion des mises à jour du binaire original — `project update` qui re-snapshotte et archive l'ancien snapshot, ou nouveau projet ? **Suggestion** : `project update` avec horodatage, les runs précédents restent valides pour l'ancien snapshot.

3. **Q-3** : Quel format pour les appels LLM capturés dans `{run}/llm/` ? JSON lignes avec `{prompt_hash, prompt, response, model, timestamp}` ?

4. **Q-4** : Le run doit-il être un sous-répertoire de `runs/` avec un UUID, ou un répertoire externe passé en argument ? **Suggestion** : sous-répertoire avec horodatage + UUID pour l'unicité, possibilité de passer `--run-dir` pour un répertoire externe.

5. **Q-5** : `re-agent build --phase transform` doit-il compiler automatiquement après la génération, ou faut-il une option `--no-compile` pour les cas où on veut seulement la source ? **Suggestion** : compiler par défaut, `--no-compile` pour ignorer.
