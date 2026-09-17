# {{PROJECT_NAME}} Implementation Guides

This directory contains implementation guides that translate RFC specifications into concrete, project-specific designs.

## Purpose

Implementation guides bridge the gap between abstract specs (RFCs) and actual code. They provide:

- Concrete module/package structure
- Type definitions with full field specifications
- Interface/trait/class definitions
- Implementation details and algorithms
- Error handling strategies
- Testing approaches

## Relationship to Specs

```
RFC Specification (abstract, what)
        |
        v
Implementation Guide (concrete, how)   <-- This directory
        |
        v
Actual Code (executable)
```

Implementation guides **supersede** RFC specs with concrete details but **MUST NOT contradict** them.

## Creating a New Guide

Use the **platonic-impl** skill to create implementation guides and implement code:

```
Use platonic-impl to implement RFC-NNN targeting the <module-name> module.
```

Or create a guide only (without coding):

```
Use platonic-impl to create a guide for RFC-NNN targeting the <module-name> module.
```

## Guide Template

Use the **platonic-impl** skill which includes its own template for generating implementation guides.

## Naming Convention

Implementation guides MUST follow the pattern `IG-XXX-<semantic-short-desc>.md`:

- `IG`: Capital letters (literal)
- `XXX`: Exactly **three numeric characters**, zero-padded (`001`–`999`). Not `1`, `01`, or `0001`.
- `<semantic-short-desc>`: Kebab-case brief description
- **Strict sequence**: Assign the next unused IG number in order (`001`, `002`, `003`, …). Do not skip numbers unless the user explicitly requests a specific number or gap.
- The IG number is independent of the source RFC number (they may match when convenient, but sequential IG allocation wins unless the user overrides)

**Examples**:
- `IG-001-user-authentication.md` - User authentication implementation
- `IG-002-message-queue-protocol.md` - Message queue protocol implementation
- `IG-003-cli-command-nesting.md` - CLI command nesting implementation
