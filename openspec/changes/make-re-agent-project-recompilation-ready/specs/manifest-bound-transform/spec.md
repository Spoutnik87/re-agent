# manifest-bound-transform

## ADDED Requirements

### Requirement: MBT-REQ-1 (Mono-Cible Liée au Manifeste)

Le Transform `preserve_abi` SHALL opérer sur exactement une fonction cible à la fois, liée à son adresse et à son entrée dans le manifeste d'analyse. Les seuls champs ABI utilisés sont ceux disponibles dans le manifeste : `address`, `name`, `signature`, `calling_convention`, `output_path`.

#### WHEN l'utilisateur exécute `re-agent build --phase transform --address 0x401234`  
THEN le système DOIT charger l'entrée du manifeste pour l'adresse `0x401234` et en extraire les champs `address`, `name`, `signature`, `calling_convention`, `output_path`.  
THEN le système DOIT générer un prompt Transform qui inclut ces champs textuellement.  
THEN le système NE DOIT PAS accéder à des champs absents du manifeste (`parameter_types`, `return_type`, `callees`, `abi_contract_sha256`) et NE DOIT PAS effectuer de validation sémantique des types ou des appelés.  

#### WHEN l'utilisateur passe plusieurs `--address` ou aucun `--address`  
THEN le système DOIT rejeter avec `ERROR: --phase transform requires exactly one --address`, DOIT sortir avec le code 1, et NE DOIT générer aucun prompt ou artefact.  

### Requirement: MBT-REQ-2 (Artefact du Transform)

Le Transform SHALL produire au moins un fichier source C++ comme artefact de sortie, écrit dans le répertoire de sortie standard.

#### WHEN le Transform retourne une fonction C++ valide  
THEN le système DOIT écrire le fichier source dans `<output_dir>/<address>__<name>.cpp` où `<output_dir>` est déterminé par la configuration du run.  
THEN le fichier DOIT commencer par un commentaire d'en-tête contenant : `// MANIFEST_BOUND`, l'adresse, le nom, la signature et la convention d'appel.  

### Requirement: MBT-REQ-3 (Compilation Obligatoire — COMPILE_PASS)

Le système SHALL compiler chaque artefact du Transform avec le compilateur du profil toolchain activé avant de l'accepter. Une compilation réussie produit COMPILE_PASS ; un échec rejette l'artefact.

#### WHEN le compilateur sort avec le code 0 sur l'artefact  
THEN le système DOIT enregistrer `COMPILE_PASS` dans le manifeste du run pour cette adresse, DOIT copier l'artefact dans `artefacts/committed/<address>/`, et DOIT y joindre le fichier objet compilé.  

#### WHEN le compilateur sort avec un code non nul  
THEN le système DOIT afficher le diagnostic du compilateur, NE DOIT PAS promouvoir l'artefact, DOIT le laisser dans `artefacts/failed/<address>/` avec le log du compilateur, et DOIT enregistrer `COMPILE_FAIL` dans le manifeste du run.  

### Requirement: MBT-REQ-4 (Aucune Validation Sémantique de Type ni de Callee)

Le système SHALL ne pas valider la correspondance des types, des paramètres, du type de retour ou de la liste des appelés entre l'artefact transformé et le binaire original. Le seul verdict binaire est MANIFEST_BOUND (artefact produit) et COMPILE_PASS (compilation réussie).

#### WHEN un artefact compile avec succès mais que sa signature C++ diffère de la `signature` textuelle du manifeste  
THEN le système DOIT tout de même enregistrer `COMPILE_PASS` et accepter l'artefact.  
THEN le système NE DOIT PAS tenter de vérifier que les types de paramètres, le type de retour ou les appelés correspondent à l'original.  

#### WHEN l'utilisateur inspecte la sortie du Transform  
THEN le verdict pour une adresse DOIT être l'un des deux suivants : `MANIFEST_BOUND` (artefact produit) ou `COMPILE_PASS` (artefact produit + compilation réussie).  
THEN le système NE DOIT pas produire de verdict `ABI_PASS`, `ABI_MISMATCH` ou équivalent.  

### Requirement: MBT-REQ-5 (Provenance et Atomicté de l'Artefact)

Le système SHALL écrire les artefacts du Transform de manière atomique et attacher des métadonnées de provenance à chaque artefact accepté.

#### WHEN un artefact passe tous les tests (MANIFEST_BOUND + COMPILE_PASS)  
THEN le système DOIT écrire l'artefact vers un chemin temporaire, puis renommer (atomique sur le même système de fichiers) vers `artefacts/committed/<address>/<address>__<name>.cpp`.  
THEN le système DOIT écrire `artefacts/committed/<address>/provenance.json` contenant `address`, `name`, `signature`, `calling_convention`, `output_path`, `model`, `temperature`, `toolchain_fingerprint`, `compile_exit_code` et `committed_at` (ISO-8601).  

#### WHEN le système plante ou perd l'alimentation pendant l'écriture de l'artefact  
THEN au redémarrage, `re-agent build --status` DOIT détecter toute écriture incomplète (artefact sans provenance, ou fichier de taille zéro), DOIT supprimer l'artefact incomplet, et DOIT marquer l'adresse comme `pending` pour une nouvelle tentative.
