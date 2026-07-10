## Task

Transform the following decompiled functions from the **{{ module_name }}** module into clean, compilable C++ code.

## Neighbouring Functions (for context only, do NOT transform)

{% for neighbour in neighbours %}
### Function {{ neighbour.address }}
```cpp
{{ neighbour.code }}
```
{% endfor %}

## Functions to Transform

{% for func in functions %}
### Function {{ func.address }}
```cpp
{{ func.code }}
```
{% endfor %}

## Expected Output Format

For each function, produce. The original function address is the stable identity — you MUST include it in every `// FILE:` path.

```
// FILE: include/<module>/0x004117c0__<descriptive_name>.h
// Original function: 0x004117c0
#pragma once
// ... header content with include guard, declarations, address comments ...

// FILE: src/<module>/0x004117c0__<descriptive_name>.cpp
#include "_decls.h"
// Original function: 0x004117c0
// ... implementation ...
```

Include ALL functions. Do not skip any. The `0x...` prefix in the FILE path is the stable identity — NEVER omit or rename it away. Follow all conventions from the system prompt.

## Critical: `_decls.h` Include Requirement

**Every emitted `.cpp` file MUST include the shared decls header as its first line of code:**

```cpp
#include "_decls.h"
```

This header provides:
- Function prototypes for every function in the binary.
- `extern` declarations for all known global variables (`DAT_*`, `PTR_*`, `g_*`, `pThis`, etc.).
- Standard includes: `<windows.h>`, `<cstdint>`, `<cstdio>`, `<ctime>`.

**Without this include, the file will NOT compile.** All globals, forward-declared symbols, and type aliases are defined in `_decls.h`. Do NOT add your own `extern` declarations for anything already there — use explicit casts at usage sites instead.
