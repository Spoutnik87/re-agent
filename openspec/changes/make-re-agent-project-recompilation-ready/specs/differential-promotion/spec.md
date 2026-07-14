# differential-promotion

## ADDED Requirements

### Requirement: DP-REQ-1 (Registre de Preuves Append-Only)

Le système SHALL maintenir un registre de preuves de promotion en annexe seule (append-only). Chaque transition est dérivée des preuves accumulées ; aucune démotion, reset ou modification mutable n'est autorisée.

#### WHEN une preuve de promotion est enregistrée  
THEN le système DOIT ajouter une ligne au fichier `<project_root>/promotion/evidence.chain` avec : `address`, `proof_type` (`abi_proven` | `differential_passed`), `run_id`, `toolchain_fingerprint`, `proof_output_path`, `recorded_at` (ISO-8601).  
THEN le système NE DOIT PAS modifier, écraser ou supprimer des lignes existantes dans `evidence.chain`.  

#### WHEN l'utilisateur tente d'exécuter une commande de démotion (par exemple `re-agent promote --address 0x401234 --demote`)  
THEN le système DOIT afficher `ERROR: promotion evidence is append-only — cannot demote. Use "re-agent promote --reset" to archive current evidence and restart`, DOIT sortir avec le code 1, et NE DOIT PAS modifier le registre.  

#### WHEN l'utilisateur exécute `re-agent promote --reset --address 0x401234`  
THEN le système DOIT archiver l'état actuel dans `<project_root>/promotion/archived/<timestamp>-0x401234.json`, DOIT créer un nouvel enregistrement vide pour l'adresse, et DOIT enregistrer la transition de reset dans `evidence.chain` comme `proof_type: reset`.  

### Requirement: DP-REQ-2 (Preuve ABI Externe)

Le système SHALL exiger une preuve ABI externe, fournie par l'adaptateur via `profile.abi_verifier.command`, avant d'enregistrer une preuve `abi_proven`. La compilation seule NE constitue PAS une preuve ABI.

#### WHEN l'utilisateur exécute `re-agent promote --address 0x401234 --proof abi`  
THEN le système DOIT invoquer la commande de `profile.abi_verifier.command` avec le fichier objet de la fonction et l'entrée du manifeste comme arguments.  
THEN si le vérifieur sort avec un code non nul, le système DOIT afficher le diagnostic du vérifieur, DOIT afficher `ERROR: ABI proof failed for 0x401234`, DOIT sortir avec le code 1, et NE DOIT PAS enregistrer de preuve.  

#### WHEN le vérifieur sort avec le code 0  
THEN le système DOIT capturer stdout et stderr, DOIT les stocker dans `<project_root>/promotion/proofs/0x401234.abi_proof.json` (avec le code de sortie, l'horodatage et l'empreinte toolchain), et DOIT enregistrer une ligne `proof_type: abi_proven` dans `evidence.chain`.  

#### WHEN `profile.abi_verifier.command` n'est pas défini dans le profil  
THEN le système DOIT afficher `ERROR: no abi_verifier in profile — cannot produce ABI proof`, DOIT sortir avec le code 1, et NE DOIT PAS appliquer de vérifieur par défaut.  

### Requirement: DP-REQ-3 (Preuve Comportementale Différentielle Externe)

Le système SHALL exiger une preuve différentielle comportementale, fournie par l'adaptateur via `profile.runtime_harness`, avant d'enregistrer une preuve `differential_passed`. Une preuve ABI seule NE suffit PAS.

#### WHEN l'utilisateur exécute `re-agent promote --address 0x401234 --proof diff`  
THEN le système DOIT exécuter `profile.runtime_harness.command` avec l'objet compilé et le binaire original comme arguments, et DOIT évaluer la sortie du harnais selon les règles définies dans le profil.  
THEN si le harnais sort avec un code non nul ou que la sortie indique une différence comportementale, le système DOIT afficher le diagnostic, DOIT afficher `ERROR: differential proof failed for 0x401234`, DOIT sortir avec le code 1, et NE DOIT PAS enregistrer de preuve.  

#### WHEN `profile.runtime_harness.command` n'est pas défini dans le profil  
THEN le système DOIT afficher `ERROR: no runtime_harness in profile — cannot produce differential proof`, DOIT sortir avec le code 1, et NE DOIT PAS recourir à un harnais intégré ni sauter cette porte.  

### Requirement: DP-REQ-4 (Preuves par Adresse, Promotion par Projet)

Le système SHALL gérer les preuves par adresse individuelle, mais SHALL supporter la promotion par projet entier, qui réussit ou échoue atomiquement.

#### WHEN l'utilisateur exécute `re-agent promote --all --proof diff`  
THEN le système DOIT itérer chaque fonction du projet et exécuter la même porte que pour l'adresse unique (DP-REQ-3) pour chaque fonction.  
THEN si la porte échoue pour une fonction quelconque, le système DOIT afficher la première erreur, DOIT sortir avec le code 1, NE DOIT PAS enregistrer de preuve pour aucune fonction, et NE DOIT PAS modifier le registre.  

#### WHEN `--all` réussit pour toutes les fonctions  
THEN le système DOIT produire un rapport récapitulatif `<project_root>/promotion/promotion_summary.json` listant chaque adresse, son type de preuve, l'horodatage, et `"all_promoted": true`.  

### Requirement: DP-REQ-5 (Vérification Fail-Closed)

Le système SHALL vérifier que toute preuve existante est cohérente avec l'environnement actuel et les données du projet avant d'autoriser des opérations aval (publication, link, build final).

#### WHEN l'empreinte toolchain dans la preuve `abi_proven` diffère de l'empreinte toolchain active  
THEN le système DOIT afficher `ERROR: proof for 0x401234 was produced by a different toolchain — re-run promotion`, DOIT refuser toute opération de build ou publication qui dépend de cette preuve, et NE DOIT PAS accepter de contournement.  

#### WHEN le fichier de preuve référencé dans `evidence.chain` est manquant ou vide  
THEN le système DOIT afficher `ERROR: proof file <path> is missing — re-run promotion for 0x401234`, DOIT archiver l'état actuel et réinitialiser l'adresse (via le mécanisme DP-REQ-1 reset), et DOIT enregistrer la réinitialisation dans `evidence.chain`.  

### Requirement: DP-REQ-6 (Aucune Logique BGE dans la Promotion)

Le système SHALL ne pas contenir de logique de vérification ABI ou différentielle propre à BGE, Jade ou tout jeu spécifique. Ces vérifications sont fournies exclusivement par l'adaptateur via le profil.

#### WHEN l'utilisateur inspecte la logique de promotion dans les sources de re-agent  
THEN elle NE DOIT contenir aucune référence à BGE, Jade, d3d9, ou tout nom de fonction spécifique à un jeu.  
THEN elle DOIT déléguer toute vérification aux commandes externes définies dans le profil (`abi_verifier.command`, `runtime_harness.command`).  

#### WHEN le profil ne définit ni `abi_verifier` ni `runtime_harness`  
THEN le système DOIT rejeter toute tentative de promotion avec `ERROR: no verifier or harness configured in profile`, DOIT sortir avec le code 1, et NE DOIT PAS appliquer de vérifieur ou harnais par défaut, quelle que soit l'adresse ou la fonction.
