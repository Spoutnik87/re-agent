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

For each function, produce:

```
// FILE: include/<module>/<ClassName>.h
// ... header content with include guard ...

// FILE: src/<module>/<ClassName>.cpp
// ... implementation ...
```

Include ALL functions. Do not skip any. Follow all conventions from the system prompt.
