# verified-project-build

## ADDED Requirements

### Requirement: VPB-REQ-1 (Build Piloté par Manifeste)

Le système SHALL orchestrer le build à partir d'un manifeste listant toutes les adresses cibles, en exigeant que chaque adresse ait un COMPILE_PASS avant de pouvoir linker.

#### WHEN l'utilisateur exécute `re-agent build --manifest <manifest.json>`  
THEN le système DOIT lire `<manifest.json>` comme un tableau d'entrées, chacune contenant au minimum : `address`, `name`, `status` (`pending | committed | failed`).  
THEN le système DOIT itérer chaque entrée dans l'ordre : pour chaque entrée `pending`, DOIT exécuter le Transform mono-cible puis la compilation ; pour chaque entrée `committed`, DOIT recompiler et vérifier le COMPILE_PASS.  
THEN le système DOIT construire une vue de couverture et refuser le link si une seule entrée n'a pas de `COMPILE_PASS`.  

#### WHEN `<manifest.json>` référence une adresse sans entrée dans le snapshot du projet  
THEN le système DOIT marquer cette entrée comme `orphan`, DOIT afficher `WARN: no manifest entry for 0x<addr> — marking orphan`, et DOIT continuer sans abandonner le build.  
THEN une entrée `orphan` DOIT compter comme un échec dans la vérification de couverture complète.  

### Requirement: VPB-REQ-2 (Staging et Publication Atomique)

Le système SHALL placer tous les artefacts du build dans un répertoire de staging isolé et les publier atomiquement vers l'arbre source engagé du projet en cas de succès ; en cas d'échec, l'arbre publié NE DOIT PAS être modifié.

#### WHEN toutes les entrées du manifeste sont traitées avec COMPILE_PASS et qu'aucune erreur fatale n'est survenue  
THEN le système DOIT déplacer (ou copier-et-vérifier) tous les artefacts de `<run_dir>/outputs/` vers un répertoire de staging `<project_root>/.staging/<run_id>/`.  
THEN le système DOIT calculer SHA-256 de chaque artefact stagé et le comparer au manifeste du run.  
THEN le système DOIT renommer (atomique) `.staging/<run_id>/` vers `<project_root>/src/committed/` (ou le chemin de sortie configuré).  
THEN le système DOIT créer `<project_root>/.last_publish.link` pointant vers l'ID du run publié.  

#### WHEN un artefact échoue à la vérification SHA-256 dans le staging  
THEN le système DOIT supprimer `.staging/<run_id>/`, NE DOIT PAS modifier `<project_root>/src/committed/`, DOIT afficher `ERROR: artefact corruption detected for 0x<addr>`, et DOIT sortir avec le code 1.  

### Requirement: VPB-REQ-3 (Recette Externe Uniquement)

Le système SHALL déléguer la compilation, l'édition de liens et toute inspection binaire à des recettes externes définies par le profil toolchain, et SHALL ne pas contenir de logique de compilation ou de link intégrée.

#### WHEN `re-agent build --link` est invoqué  
THEN le système DOIT exécuter la commande spécifiée dans `profile.linker.command` avec `profile.linker.flags`, en passant la liste des fichiers objets engagés et le chemin de sortie via les variables de template `{objects}` et `{output}`.  
THEN le système DOIT capturer stdout, stderr et le code de sortie du linker.  
THEN si le linker sort avec un code non nul, le système DOIT afficher le diagnostic complet du linker, NE DOIT PAS produire d'artefact lié et DOIT sortir avec le code 1.  

#### WHEN `profile.linker.command` n'est pas défini dans le profil  
THEN le système DOIT afficher `ERROR: no linker.command in profile — cannot link`, DOIT sortir avec le code 1, et NE DOIT PAS tenter un link interne ou un link avec `ld` par défaut.  

#### WHEN `profile.compiler.command` référence un binaire qui n'existe pas ou n'est pas exécutable  
THEN le système DOIT échouer avant l'invocation avec `ERROR: compiler.command "<path>" is not executable`, DOIT sortir avec le code 1, et NE DOIT PAS recourir à un compilateur par défaut.  

### Requirement: VPB-REQ-4 (Échec sur Couverture Incomplète)

Le système SHALL refuser de produire un artefact lié à moins que la couverture du manifeste ne soit à 100 % COMPILE_PASS, ou que l'utilisateur n'accepte explicitement une couverture partielle via `--allow-partial`.

#### WHEN `re-agent build --link` s'exécute et que le manifeste a des entrées sans COMPILE_PASS  
THEN le système DOIT afficher la liste des entrées non-compilées avec leur statut, DOIT afficher `ERROR: cannot link — <N> entries without COMPILE_PASS (use --allow-partial to override)`, DOIT sortir avec le code 1, et NE DOIT PAS produire d'artefact lié.  

#### WHEN `--allow-partial` est passé  
THEN le système DOIT afficher un avertissement visible `WARN: --allow-partial: linked artefact will be incomplete`, DOIT linker quand même, et DOIT enregistrer `partial: true` dans le manifeste du run et dans la provenance de l'artefact lié.  

### Requirement: VPB-REQ-5 (Aucune Règle de Build BGE)

Le système SHALL ne pas embarquer de script de link, fichier de spécifications GCC, fichier de ressources Windows ou code de DLL proxy dans sa logique de build.

#### WHEN l'utilisateur inspecte les fichiers sources du pipeline de build  
THEN aucun NE DOIT contenir les chaînes `BGE`, `JADE`, `bge`, `jade`, `d3d9`, `proxy`, `.def`, `MinGW`, `msys` ou `-m32` en tant que chaînes codées en dur, sauf si explicitement requis pour la validation du schéma de profil toolchain ou dans des fixtures de test.  

#### WHEN le profil inclut `-m32` dans `compiler.flags`  
THEN ce drapeau DOIT apparaître UNIQUEMENT parce que le profil l'énumère explicitement ; le système NE DOIT PAS l'injecter depuis une valeur par défaut interne.  

### Requirement: VPB-REQ-6 (Provenance de l'Artefact de Build)

Le système SHALL attacher un manifeste de provenance à chaque artefact lié, énumérant chaque adresse source, son statut COMPILE_PASS et l'empreinte toolchain complète.

#### WHEN un artefact lié est produit avec succès  
THEN le système DOIT écrire `<output>.provenance.json` à côté de l'artefact, contenant : `project_id`, `run_id`, `toolchain_fingerprint`, `coverage` (`total`/`with_compile_pass`), `partial` (booléen), `link_command`, `link_exit_code`, et un tableau `sources` avec chaque `{address, name, compile_pass: true}`.  

#### WHEN l'utilisateur exécute `re-agent build verify --artefact <path>`  
THEN le système DOIT charger `<path>.provenance.json`, DOIT vérifier l'empreinte toolchain par rapport à l'environnement actuel, DOIT vérifier que chaque `compile_pass` dans `sources` correspond à un COMPILE_PASS dans le projet, et DOIT afficher `PASS` si tout est conforme ou lister chaque échec avec les détails.
