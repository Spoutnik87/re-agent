Rename variables, types, and data labels in this reverse-engineered function.

**Function:** ${class_name}::${function_name} at ${address}

**Code to rename (control flow is CORRECT — only rename identifiers and types):**
```cpp
${code}
```

Instructions:
1. Replace ALL Ghidra variable names (local_XX, iVarX, uVarX, fVarX, pfVarX, puVarX, DAT_XXXX) with descriptive C++ names
2. Replace Ghidra types (undefined4 → uint32_t, undefined8 → uint64_t, float10 → long double, etc.)
3. Add a brief `//` comment explaining each renamed variable at its declaration
4. DO NOT change control flow, function calls, arithmetic, or logic — ONLY rename
5. Output the complete renamed function

```cpp
<clean renamed C++23 code>
```
