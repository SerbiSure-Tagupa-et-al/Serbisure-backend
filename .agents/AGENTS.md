# Agent Rules for Serbisure Django Backend

## Mobile release safety and version API
- Public, read-only `GET /api/v1/app-version/` fetches the latest stable universal APK from `SerbiSure-Tagupa-et-al/SerbiSure-Frontend`. External requests belong in `core/app_releases.py`, not the view.
- Cache successful metadata for 600 seconds using the dedicated process-local LocMemCache. Cache upstream failures for 30 seconds; return 503 rather than pretending there is no update. Each worker has its own memory cache.
- Do not add version models, columns, database cache tables or migrations. No backend version edit is required for each release.
- Only expose official GitHub universal APK URLs. Never select ARM-only assets, drafts, prereleases or incomplete uploads as the current release. No published release is an explicit null response, not a fake version.
- Keep response field names compatible with the mobile update service. Installed version comparisons happen on the device using its native application version.
- Never weaken, disable or falsify release workflows, test assertions or required checks to get a failing PR merged. Fix the root cause and retain coverage. Do not deploy or publish to test a change.

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

## Security Rules (API-Security-Checklist)
*Reference: [API Security Checklist](https://github.com/shieldfy/API-Security-Checklist) - Refer to this link for the comprehensive guide.*

### Authentication
- Don't use Basic Auth. Use standard authentication instead.
- Don't reinvent the wheel in Authentication, token generation, password storage. Use the standards.
- Use Max Retry and jail features in Login.
- Use encryption on all sensitive data.

### Access
- Limit requests (Throttling) to avoid DDoS / brute-force attacks.
- Use HTTPS on server side with TLS 1.2+ and secure ciphers to avoid MITM (Man in the Middle Attack) and ensure Host header matches the SNI.
- Use HSTS header with SSL to avoid SSL Strip attacks.
- Turn off directory listings.
- For private APIs, allow access only from safelisted IPs/hosts.

### Authorization
- OAuth: Always validate redirect_uri server-side to allow only safelisted URLs.
- Always try to exchange for code and not tokens (don't allow response_type=token).
- Use state parameter with a random hash to prevent CSRF on the OAuth authorization process.
- Define the default scope, and validate scope parameters for each application.

### Input
- Use the proper HTTP method according to the operation.
- Validate content-type on request Accept header (Content Negotiation).
- Validate content-type of posted data.
- Validate user input to avoid common vulnerabilities (e.g., XSS, SQL-Injection, Remote Code Execution, etc.).
- Don't use any sensitive data (credentials, Passwords, security tokens, or API keys) in the URL, but use standard Authorization header.
- Use only server-side encryption.
- Use an API Gateway service to enable caching and Rate Limit policies.

### Processing
- Check if all the endpoints are protected behind authentication.
- User own resource ID should be avoided. Use /me/orders instead of /user/654321/orders.
- Don't auto-increment IDs. Use UUID instead.
- If parsing XML/YAML, disable entity expansion to avoid XXE/Billion Laughs attacks.
- Use a CDN for file uploads.
- Use Workers and Queues for huge amounts of data to avoid HTTP Blocking.
- Do not forget to turn the DEBUG mode OFF.
- Use non-executable stacks when available.

### Output
- Send X-Content-Type-Options, X-Frame-Options, and Content-Security-Policy headers.
- Remove fingerprinting headers (X-Powered-By, Server, etc.).
- Force content-type for your response.
- Do not return overly specific error messages to the client that could reveal implementation details.
- Don't return sensitive data like credentials, passwords, or security tokens.
- Return the proper status code according to the operation completed.

### CI & CD / Monitoring / Advanced
- Audit your design with unit/integration tests coverage.
- Continuously run security tests and check dependencies for vulnerabilities.
- Ensure you aren't logging any sensitive data like credit cards, passwords, PINs, etc.
- Rate Limiting: Implement sliding window rate limiting.
- Secrets Management: Never commit secrets to version control - use environment variables or secret managers.

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
