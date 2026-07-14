# target-toolchain-contracts

## ADDED Requirements

### Requirement: TC-REQ-1 (Schéma de Profil Toolchain)

Le système SHALL définir un schéma formel pour les profils toolchain qui déclare backend, cible, compilateur, linker et capabilities optionnelles — sans aucune valeur par défaut intégrée au cœur.

#### WHEN l'utilisateur exécute `re-agent toolchain schema`  
THEN le système DOIT afficher un JSON Schema contenant au minimum : `backend` (chaîne), `target` (chaîne), `compiler.command` (chaîne), `compiler.flags` (tableau de chaînes).  
THEN les champs suivants DOIVENT être marqués comme optionnels : `linker.command`, `linker.flags`, `runtime_harness`, `binary_inspector.command`, `abi_verifier.command`, `differential_matcher`.  
THEN le schéma DOIT inclure une propriété `extensions` de type `object` avec `additionalProperties: true` pour les clés spécifiques à l'adaptateur.  
THEN le schéma NE DOIT contenir aucun champ nommé `bge_`, `jade_`, `gog_` ou toute clé impliquant un jeu spécifique.  

### Requirement: TC-REQ-2 (Validation Stricte du Profil)

Le système SHALL valider tout profil toolchain contre le schéma au chargement, rejeter les clés inconnues (hors `extensions`) et refuser tout profil invalide avec un diagnostic précis.

#### WHEN l'utilisateur passe `--profile <path>` et que le fichier contient une clé inconnue en dehors de `extensions` (par exemple `bge_arch`)  
THEN le système DOIT sortir avec le code 1, DOIT afficher `ERROR: unknown key "bge_arch" — valid keys are: backend, target, compiler, linker, runtime_harness, binary_inspector, abi_verifier, differential_matcher, extensions`, et NE DOIT PAS procéder à quelque opération que ce soit.  

#### WHEN le profil omet un champ requis (`backend` ou `target` ou `compiler`)  
THEN le système DOIT sortir avec le code 1, DOIT afficher `ERROR: profile.<key> is required`, et NE DOIT PAS procéder.  

#### WHEN le profil contient des clés inconnues dans `extensions`  
THEN le système DOIT les accepter sans erreur ni avertissement, car `extensions` est un espace réservé à l'adaptateur.  

### Requirement: TC-REQ-3 (Empreinte Toolchain)

Le système SHALL capturer et persister une empreinte des binaires de la toolchain (compilateur, et optionnellement linker, inspecteur, vérifieur) au moment de l'activation du profil.

#### WHEN `re-agent toolchain activate --profile <profil-valide>` réussit  
THEN le système DOIT calculer SHA-256 de chaque binaire listé dans `compiler.command`, et optionnellement dans `linker.command`, `binary_inspector.command`, `abi_verifier.command` s'ils sont présents.  
THEN le système DOIT écrire `<project_root>/toolchain/<profile_hash>/fingerprint.json` contenant chaque chemin de commande, son SHA-256, et le hash du contenu du profil.  
THEN le système DOIT rejeter l'activation si `compiler.command` n'existe pas ou n'est pas exécutable.  
THEN si une capability optionnelle (`abi_verifier`, `runtime_harness`) référence un binaire manquant, le système DOIT afficher un avertissement mais NE DOIT PAS refuser l'activation.  

### Requirement: TC-REQ-4 (Échec sur Divergence Toolchain)

Le système SHALL comparer l'empreinte stockée de la toolchain à l'environnement actuel avant chaque opération reverse, transform ou build qui dépend de la toolchain, et abandonner en cas de divergence.

#### WHEN l'utilisateur exécute `re-agent build` et que le SHA-256 d'un binaire de la toolchain diffère de `fingerprint.json`  
THEN le système DOIT afficher `ERROR: toolchain binary <path> has changed (expected <hash>, got <hash>)`, DOIT sortir avec le code 1, et NE DOIT PAS invoquer le compilateur, linker, inspecteur ou vérifieur.  

#### WHEN l'utilisateur exécute `re-agent build` et que `fingerprint.json` est absent ou corrompu  
THEN le système DOIT afficher `ERROR: no toolchain fingerprint — run "re-agent toolchain activate" first`, DOIT sortir avec le code 1, et NE DOIT PAS démarrer de phase du pipeline.  

### Requirement: TC-REQ-5 (Provenance du Profil)

Le système SHALL enregistrer la provenance de chaque profil toolchain activé : sa source, son horodatage de modification et son hash de contenu au moment de l'activation.

#### WHEN `re-agent toolchain activate` réussit  
THEN le système DOIT enregistrer dans `<project_root>/toolchain/active.link` un objet JSON avec `"source"`, `"activated_at"` (ISO-8601), `"profile_sha256"` et `"fingerprint_sha256"`.  
THEN une exécution ultérieure de `re-agent toolchain status` DOIT afficher ces champs et DOIT avertir si `profile_sha256` diffère du profil actuel sur le disque à `"source"`.  

### Requirement: TC-REQ-6 (Aucune Toolchain Implicite)

Le système SHALL ne pas utiliser de compilateur, linker, flag d'architecture ou environnement d'exécution par défaut quand aucun profil n'est fourni.

#### WHEN l'utilisateur exécute une commande `re-agent` sans `--profile` et sans profil activé sur le disque  
THEN le système DOIT afficher `ERROR: no toolchain profile active. Provide --profile or run "re-agent toolchain activate --profile <path>"`, DOIT sortir avec le code 1, et NE DOIT PAS recourir à un réglage implicite (MinGW, `-m32`, `i686` ou autre).  

#### WHEN l'utilisateur fournit `--profile` avec `target: "x86_64-pc-linux-gnu"` et `compiler.flags: ["-O2"]`  
THEN le système DOIT utiliser ces valeurs textuellement, NE DOIT PAS injecter `-m32`, et NE DOIT PAS ajouter de drapeaux BGE.
