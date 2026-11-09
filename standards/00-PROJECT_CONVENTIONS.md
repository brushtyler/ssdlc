# Project conventions

Use the target repository's instructions, configuration, architecture and existing
components as the source of project conventions. Identify applicable rules and
verification commands before proposing changes.

- Reuse established interfaces, utilities and components before adding equivalents.
- Follow existing naming, formatting, error handling and logging conventions.
- Preserve public contracts, compatibility and security invariants unless the
  approved requirements explicitly authorize changing them.
- Verify behavior with the project's configured checks and review test changes
  with the same care as implementation changes.
- Report conflicting instructions, missing checks and necessary scope changes.
  Do not suppress checks or invent frameworks, paths, tokens or UI primitives.

Project-specific adapters can add concrete rules. This baseline does not prescribe
a programming language, framework, design system or application domain.
