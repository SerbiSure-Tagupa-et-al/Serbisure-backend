# Agent Rules for Serbisure Django Backend

## Project Overview
- Backend application built with Django and Django REST Framework.
- Serbisure focuses on service booking and management workflows.
- Emphasis on scalability, maintainability, and clean architecture.

## Core Development Principles
- App-based folder structure reflecting domain features instead of monolithic chaos.
- Reusable utilities and services only when actually reusable, not "just in case".
- Strict separation of API Layer (Views), Business Logic (Services/Managers), and Data Models.
- Avoid circular dependencies (they always come back somehow, especially in models).
- Keep database transactions and state management simple and predictable.

## Code Quality & Static Analysis Tools
- **Ruff / Vulture (Dead Code & Linting)**
  - Detects unused imports, variables, and dead code.
  - Helps eliminate dead code before it becomes archaeological evidence.
  - Run before merging to keep the project clean and minimal.
- **Django-Extensions graph_models (Graphing)**
  - Generates dependency graphs for the project models.
  - Used to visualize app relationships and detect circular dependencies.
  - Helps enforce clean architectural boundaries between features.
- **Custom Exception Handler (Error Consistency)**
  - Ensures consistent error handling patterns across the API.
  - Standardizes API errors, validation errors, and server failures.
  - Prevents inconsistent error handling like random unhandled exceptions.

## Architecture Rules
- Feature modules (Django apps) should be focused and domain-specific.
- Shared utilities and mixins live in a central `core` or `common` app.
- Business logic isolated in `services.py` or model managers, NOT in `views.py`.
- Serializers should handle strict validation, not business logic.
- No direct external API calls inside views (use the service layer).

## Database & ORM Guidelines
- Prefer efficient querying: always use `select_related` and `prefetch_related` to prevent N+1 issues.
- Avoid unnecessary database transactions for read-heavy operations.
- Derived data/aggregations should be handled at the database level where appropriate.

## API & Data Handling
- All API interactions must go through DRF Serializers.
- Normalize API responses (consistent success/error payload structures).
- Do not leak internal model structures or raw traceback responses to the frontend.

## Performance Rules
- Avoid unnecessary database hits in loops.
- Use query optimization where it actually improves performance.
- Keep model dependency graphs clean.

## Testing & Validation
- Unit tests required for core services and model logic.
- API tests for critical booking and management flows.
- Run linters before every merge.
- Model graph checks must pass (no circular dependencies).
- Error handling must be used consistently.

## Build Discipline
- No unused imports or dead code in the main branch.
- No circular dependencies allowed.
- No inconsistent error handling.
- If tools or tests report issues, fix them before pretending everything is fine.
