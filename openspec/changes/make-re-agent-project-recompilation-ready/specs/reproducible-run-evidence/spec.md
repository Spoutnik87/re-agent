# reproducible-run-evidence

## ADDED Requirements

### Requirement: RRE-REQ-1 (Isolation et Verrouillage des Runs)

Chaque run SHALL s'exécuter dans un répertoire isolé, verrouillé par un verrou OS (`LockFile`/`flock`) complété par des métadonnées de run dans le fichier de verrou.

#### WHEN l'utilisateur démarre une opération transform, build ou verify  
THEN le système DOIT créer `<project_root>/runs/<run_id>/` où `<run_id>` est `YYYYMMDDTHHMMSS-<random-8>`.  
THEN le système DOIT acquérir un verrou OS sur `runs/<run_id>/.lock` (via `LockFile` sur Windows ou `flock` sur POSIX).  
THEN le système DOIT écrire dans `.lock` un objet JSON contenant `run_id`, `pid`, `started_at` (ISO-8601), `command` et `user` avant de procéder.  
THEN un run concurrent NE DOIT PAS pouvoir écrire dans le même `<run_id>` ; si le verrou est maintenu plus de 24 heures, le système PEUT le briser sur `--force` explicite avec un avertissement.  

#### WHEN le verrou OS ne peut pas être acquis (déjà verrouillé)  
THEN le système DOIT afficher `ERROR: run <run_id> is already locked by PID <pid> since <started_at>`, DOIT sortir avec le code 1, et NE DOIT PAS modifier le répertoire du run.  

### Requirement: RRE-REQ-2 (Capture Complète des Entrées et Sorties)

Le système SHALL capturer chaque entrée et sortie externe d'un run — configuration, profil, contrat ABI, paires requête/réponse LLM complètes — sous forme de fichiers lisibles par machine.

#### WHEN un run transform se termine  
THEN `<run_dir>/inputs/` DOIT contenir : `profile.yaml`, `manifest_entry.json` (adresse, nom, signature, convention d'appel), `toolchain_fingerprint.json`, et `llm_exchanges.jsonl` (un objet JSON par appel LLM contenant `request.prompt`, `request.model`, `request.temperature`, `request.timestamp`, `response.content` intégral, `response.timestamp`, `response.tokens_in`, `response.tokens_out`).  
THEN `<run_dir>/outputs/` DOIT contenir chaque artefact généré pendant le run, organisé par adresse.  
THEN `<run_dir>/logs/` DOIT contenir `compiler.log`, `transformer.log`.  

#### WHEN un run build se termine  
THEN `<run_dir>/inputs/` DOIT contenir en supplément : `manifest.json` complet, `profile.yaml`, `toolchain_fingerprint.json`.  
THEN `<run_dir>/outputs/` DOIT contenir l'artefact lié final (si réussi) ou le répertoire d'objets partiel (si échoué).  

### Requirement: RRE-REQ-3 (Replay Hors Ligne)

Le système SHALL rejouer tout run à partir de ses entrées capturées sans accès réseau (pas d'appel API LLM, pas d'inspection binaire externe), en utilisant les réponses précédemment stockées.

#### WHEN l'utilisateur exécute `re-agent run replay --run-id <run_id>`  
THEN le système DOIT charger les réponses LLM depuis `<run_dir>/inputs/llm_exchanges.jsonl` au lieu d'appeler une API LLM.  
THEN le système DOIT charger les entrées du manifeste depuis `<run_dir>/inputs/manifest_entry.json` au lieu de les recalculer depuis le snapshot.  
THEN le système DOIT vérifier que le SHA-256 de l'artefact rejoué correspond au SHA-256 de l'artefact original enregistré dans le manifeste du run ; en cas de divergence, le système DOIT afficher `ERROR: replay mismatch for <address>` et sortir avec le code 1.  

#### WHEN le répertoire du run manque un fichier d'entrée requis (`profile.yaml`, `manifest_entry.json`, `llm_exchanges.jsonl`)  
THEN `replay` DOIT abandonner avec `ERROR: incomplete run evidence — missing <file>`, DOIT sortir avec le code 1, et NE DOIT PAS tenter de générer des données depuis des sources live.  

### Requirement: RRE-REQ-4 (Reprise Fail-Closed)

Le système SHALL permettre la reprise d'un run échoué depuis le dernier point de contrôle persisté, et SHALL refuser la reprise si le verrou, les entrées ou l'empreinte toolchain ont changé.

#### WHEN un run transform échoue en cours de route et que l'utilisateur exécute `re-agent run resume --run-id <run_id>`  
THEN le système DOIT vérifier que `<run_dir>/.lock` est libre (pas de processus actif), que l'empreinte toolchain correspond à l'environnement actuel, et que le hash du profil correspond.  
THEN le système DOIT recharger le manifeste du run, DOIT sauter les adresses déjà marquées `committed`, DOIT réessayer les adresses marquées `failed` ou `pending`, et DOIT ajouter les nouveaux artefacts sans modifier ceux déjà engagés.  

#### WHEN une vérification de verrou, d'empreinte ou de profil échoue  
THEN le système DOIT afficher la divergence spécifique, DOIT sortir avec le code 1, et NE DOIT PAS écraser de fichier dans `<run_dir>`.  

### Requirement: RRE-REQ-5 (Chaîne de Hash des Preuves)

Le système SHALL maintenir une chaîne de hash allant de l'identité du projet à chaque run, rendant chaque preuve de run inviolable.

#### WHEN un run se termine  
THEN le système DOIT calculer SHA-256 du répertoire des entrées (chemins triés, contenu trié) et du répertoire des sorties, DOIT les combiner en un hash de run, et DOIT ajouter à `<project_root>/runs/evidence.chain` une ligne : `<run_id> <prev_hash> <run_hash>`.  
THEN le `<prev_hash>` du premier run DOIT être le contenu de `project.id`.  
THEN une exécution ultérieure de `re-agent run verify --run-id <run_id>` DOIT recalculer le hash du run et le comparer à la chaîne, en affichant `PASS` ou `FAIL: evidence chain broken at <run_id>`.  

### Requirement: RRE-REQ-6 (Aucune Règle BGE dans les Preuves)

Le système SHALL ne pas embarquer d'identifiants, chemins ou variables d'environnement BGE, Jade ou spécifiques à un jeu dans les preuves ou métadonnées de run.

#### WHEN l'utilisateur inspecte tout champ dans `<run_dir>/inputs/`, `<run_dir>/logs/` ou `evidence.chain`  
THEN aucun NE DOIT contenir `BGE`, `JADE`, `bge`, `jade`, `gog`, `GOG`, `d3d9` ou tout chemin de DLL Windows à moins d'avoir été explicitement fourni par l'utilisateur dans le profil.  

#### WHEN le run implique un profil qui cible `i686-w64-mingw32`  
THEN les preuves du run DOIVENT enregistrer le triplet tel quel, NE DOIVENT pas le réécrire en alias interne, et NE DOIVENT pas déduire de drapeaux depuis le triplet.
