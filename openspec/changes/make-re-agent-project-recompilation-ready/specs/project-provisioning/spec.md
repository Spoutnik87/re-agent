# project-provisioning

## ADDED Requirements

### Requirement: PP-REQ-1 (Project Identity)

Le système SHALL dériver une identité de projet unique à partir du binaire cible et de son snapshot d'analyse, en acceptant tout nom ou métadonnée fourni par l'utilisateur sans validation sémantique.

#### WHEN l'utilisateur exécute `re-agent project provision --binary <path> --analysis <path> --name <user-name>`  
THEN le système DOIT calculer SHA-256 du binaire et SHA-256 du manifeste du snapshot d'analyse, DOIT combiner les deux en un fingerprint déterministe, et DOIT persister le fingerprint dans `<project_root>/project.id`.  
THEN le système DOIT copier le snapshot d'analyse dans `<project_root>/snapshots/<fingerprint>/` et y inclure un fichier `snapshot.sha256` listant chaque fichier avec son SHA-256.  
THEN `<user-name>` DOIT être stocké textuellement dans `project.id` sans transformation, validation ou rejet, quel que soit son contenu.  

### Requirement: PP-REQ-2 (Provisionnement Fail-Closed)

Le système SHALL abandonner le provisionnement sans laisser d'état partiel si une entrée requise est absente, invalide ou illisible.

#### WHEN `--binary`, `--analysis` ou `--output` est omis  
THEN le système DOIT sortir avec le code 1, DOIT afficher un diagnostic listant chaque argument manquant, et NE DOIT PAS créer de fichier ou répertoire sous `--output`.  

#### WHEN le binaire est introuvable ou son SHA-256 ne correspond pas au champ `binary_sha256` du snapshot d'analyse  
THEN le système DOIT rejeter le provisionnement avec une erreur `fingerprint mismatch`, NE DOIT PAS écrire `project.id`, et NE DOIT PAS créer de répertoire de run ou de snapshot.  

### Requirement: PP-REQ-3 (Snapshot Isolé)

Le système SHALL copier le snapshot d'analyse dans un répertoire versionné, garantissant que toute modification externe de la source n'affecte pas le projet.

#### WHEN le provisionnement réussit  
THEN le système DOIT créer `<project_root>/snapshots/<fingerprint>/` contenant une copie complète des fichiers JSON d'analyse et un manifeste `snapshot.sha256`.  
THEN `snapshot.sha256` DOIT lister chaque fichier avec son SHA-256.  
THEN toute passe d'analyse ultérieure DOIT lire depuis ce snapshot, jamais depuis le `--analysis` original.  

### Requirement: PP-REQ-4 (Aucun Profil par Défaut)

Le système SHALL ne pas injecter de profil par défaut, de règle de toolchain, de macro ou de chemin spécifique à une cible.

#### WHEN le provisionnement s'exécute sans `--profile`  
THEN le système NE DOIT PAS créer de répertoire `profile/` et NE DOIT PAS initialiser de configuration de compilateur, de flags ou de backend.  
THEN le système DOIT afficher `INFO: no profile set — use "re-agent toolchain activate" before building`.  

#### WHEN l'utilisateur inspecte les fichiers créés par le provisionnement  
THEN `<project_root>/` DOIT contenir exactement : `project.id`, `snapshots/<fingerprint>/`.  
THEN aucun fichier NE DOIT contenir les chaînes `BGE`, `JADE`, `bge`, `jade`, `-m32`, `i686` ou `gog` à moins qu'elles n'aient été explicitement fournies par l'utilisateur.  

### Requirement: PP-REQ-5 (Sortie Atomique)

Le système SHALL écrire tous les artefacts du provisionnement de manière atomique : soit le projet est entièrement peuplé et valide, soit il n'existe pas.

#### WHEN le provisionnement échoue après avoir partiellement écrit des fichiers  
THEN le système DOIT détecter l'échec, DOIT supprimer `<project_root>` et tout contenu partiel, et DOIT retourner le code 1.  
THEN aucun répertoire nommé d'après le projet tenté NE DOIT subsister sur le disque.  

#### WHEN le provisionnement réussit  
THEN une réexécution avec des entrées identiques DOIT produire un `project.id` et un `snapshots/` bit-identiques.
