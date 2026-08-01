---
name: coding-standards
description: Baseline cross-project coding conventions for naming, readability, immutability, and code-quality review. Use detailed frontend or backend skills for framework-specific patterns.
---

# Coding Standards & Best Practices

Baseline coding conventions applicable across all projects.

## Core Philosophy

When multiple solutions exist, prefer the one that is, in order:

1. Correct
2. Readable
3. Maintainable
4. Testable
5. Performant

Never sacrifice readability for a small performance gain unless the performance issue has been measured.

## When to Activate

- Starting a new project or module
- Reviewing code for quality and maintainability
- Refactoring existing code to follow conventions
- Enforcing naming, formatting, or structural consistency
- Setting up linting, formatting, or type-checking rules
- Onboarding new contributors to coding conventions

## Scope Boundaries

Activate this skill for:
- Descriptive naming
- Immutability defaults
- Readability, KISS, DRY, and YAGNI enforcement
- Error-handling expectations and code-smell review

Do not use this skill as the primary source for:
- React composition, hooks, or rendering patterns
- Backend architecture, API design, or database layering
- Domain-specific framework guidance

## Code Quality Principles

### 1. Readability First
- Code is read far more often than it is written.
- Use clear variable and function names.
- Prefer self-documenting code over comments.
- Keep formatting consistent.

### 2. KISS (Keep It Simple, Stupid)
- Use the simplest solution that works.
- Avoid over-engineering.
- Avoid premature optimization.
- Prefer easy to understand over clever.

### 3. DRY (Don't Repeat Yourself)
- Extract common logic into functions.
- Create reusable components.
- Share utilities across modules.
- Avoid copy-paste programming.

### 4. YAGNI (You Aren't Gonna Need It)
- Don't build features before they're needed.
- Avoid speculative generality.
- Add complexity only when required.
- Start simple, refactor when needed.

## Coding Standards

The following examples are written in TypeScript but apply to any language.

### Variable Naming

```typescript
// PASS: GOOD: Descriptive names
const marketSearchQuery = 'election'
const isUserAuthenticated = true
const totalRevenue = 1000

// FAIL: BAD: Unclear names
const q = 'election'
const flag = true
const x = 1000
```

- Use nouns for types.
- Use verbs for functions.
- Prefix booleans with:
  - `is`
  - `has`
  - `can`
  - `should`
  - `will`

Examples:

- `isAuthenticated`
- `hasPermission`
- `canRetry`
- `shouldRefresh`
- `willExpire`

Collections should be plural: `users`, `orders`, `products`.

Single items should be singular: `user`, `order`, `product`.

## Function Design

Functions should:

- Do one thing.
- Have a descriptive name.
- Stay under 30 lines where possible.
- Take fewer than 4 parameters.
- Return a single responsibility.
- Avoid hidden side effects.

### Immutability Pattern (CRITICAL)

Prefer immutable updates by default.

Mutation is acceptable when it is:

- Required for performance
- Local to a function
- Expected by the language's idioms

Never mutate shared state unexpectedly.

### Error Handling

Treat errors as values rather than exceptions, and always handle the error first.

Languages like Go, Rust, and Zig already treat errors as values natively. In TypeScript, which relies on try/catch, use small helpers to get the same effect:

```typescript
type Handler<T> = [T, null] | [null, Error];

export const returnHandler = <T>(result: T): Handler<T> => {
  return [result, null] as const;
};

export const errorHandler = (error: unknown): [null, Error] => {
  if (error instanceof Error) {
    console.error(error);
    return [null, error] as const;
  }

  const wrappedError = new Error(
    `Unknown error ${JSON.stringify(error)} ${String(error)}`,
  );
  console.error(wrappedError);
  return [null, wrappedError] as const;
};
```

Errors should never be ignored. Prefer explicit error handling.

When the language supports errors as values (Go, Rust, Zig), use that idiom.

When using exceptions, catch only when you can recover or add context.

```typescript
// PASS: GOOD: Comprehensive error handling
async function fetchData(url: string) {
  const [response, responseError] = await fetch(url)
    .then(returnHandler)
    .catch(errorHandler);

  if (responseError) {
    // handle it
  }

  const [json, jsonError] = await response.json()
    .then(returnHandler)
    .catch(errorHandler)

  if (jsonError) {
    // handle it
  }

  return json
}

// FAIL: BAD: No error handling
async function fetchData(url) {
  const response = await fetch(url)
  return response.json()
}
```

### Async/Await Best Practices

```typescript
// PASS: GOOD: Parallel execution when possible
const [users, markets, stats] = await Promise.all([
  fetchUsers(),
  fetchMarkets(),
  fetchStats()
])

// FAIL: BAD: Sequential when unnecessary
const users = await fetchUsers()
const markets = await fetchMarkets()
const stats = await fetchStats()
```

### Type Safety

```typescript
// PASS: GOOD: Proper types
interface Market {
  id: string
  name: string
  status: 'active' | 'resolved' | 'closed'
  created_at: Date
}

function getMarket(id: string): Promise<Market> {
  // Implementation
}

// FAIL: BAD: Using 'any'
function getMarket(id: any): Promise<any> {
  // Implementation
}
```

Prefer reusing or extending existing types. Only create a new type when no suitable type already exists.

## Comments & Documentation

### When to Comment

Write comments only about what the code itself cannot explain.

Comments should explain:

- Why
- Tradeoffs
- Business rules
- Unusual behavior

Never explain syntax.

```typescript
// PASS: GOOD: Explain WHY, not WHAT
// Use exponential backoff to avoid overwhelming the API during outages
const delay = Math.min(1000 * Math.pow(2, retryCount), 30000)

// Deliberately using mutation here for performance with large arrays
items.push(newItem)

// FAIL: BAD: Stating the obvious
// Increment counter by 1
count++

// Set name to user's name
name = user.name
```

## Testing Standards

Tests should verify behavior, not implementation.

Refactoring should never require rewriting tests.

### Test Structure (AAA Pattern)

```typescript
test('calculates similarity correctly', () => {
  // Arrange
  const vector1 = [1, 0, 0]
  const vector2 = [0, 1, 0]

  // Act
  const similarity = calculateCosineSimilarity(vector1, vector2)

  // Assert
  expect(similarity).toBe(0)
})
```

### Test Naming

```typescript
// PASS: GOOD: Descriptive test names
test('returns empty array when no markets match query', () => { })
test('throws error when OpenAI API key is missing', () => { })
test('falls back to substring search when Redis unavailable', () => { })

// FAIL: BAD: Vague test names
test('works', () => { })
test('test search', () => { })
```

## Code Smell Detection

Watch for these anti-patterns:

### 1. Long Functions

```typescript
// FAIL: BAD: Function > 50 lines
function processMarketData() {
  // 100 lines of code
}

// PASS: GOOD: Split into smaller functions
function processMarketData() {
  const validated = validateData()
  const transformed = transformData(validated)
  return saveData(transformed)
}
```

### 2. Deep Nesting

```typescript
// FAIL: BAD: 5+ levels of nesting
if (user) {
  if (user.isAdmin) {
    if (market) {
      if (market.isActive) {
        if (hasPermission) {
          // Do something
        }
      }
    }
  }
}

// PASS: GOOD: Early returns
if (!user) return
if (!user.isAdmin) return
if (!market) return
if (!market.isActive) return
if (!hasPermission) return

// Do something
```

### 3. Magic Numbers

```typescript
// FAIL: BAD: Unexplained numbers
if (retryCount > 3) { }
setTimeout(callback, 500)

// PASS: GOOD: Named constants
const MAX_RETRIES = 3
const DEBOUNCE_DELAY_MS = 500

if (retryCount > MAX_RETRIES) { }
setTimeout(callback, DEBOUNCE_DELAY_MS)
```

**Remember**: Code quality is not negotiable. Clear, maintainable code enables rapid development and confident refactoring.

Always prefer guard clauses.

```typescript
// FAIL: BAD
if (user) {
  if (user.admin) {
    // ...
  }
}

// PASS: GOOD
if (!user) return
if (!user.admin) return
```

Never duplicate:

- URLs
- API routes
- Environment variable names
- Timeout values
- Status strings

Extract them into named constants instead.

Functions should either:

- Calculate something, or
- Perform a side effect

Avoid mixing both whenever possible.

Handle `null` and `undefined` explicitly. Prefer early validation. Avoid deep optional chaining that hides missing data.

## Logging

Logs should answer:

- What happened?
- Why?
- Which resource?

Avoid vague logs like `"Error"`.

Prefer specific logs like `"Failed to create order 1832"`.

Never log:

- Passwords
- Secrets
- Tokens
- API keys
- Personal information

Never hardcode:

- Credentials
- API keys
- Ports
- Environment values

Use configuration files or environment variables instead.

## Performance

Do not optimize code before measuring. Readable O(n) code is often better than unreadable, micro-optimized code. Optimize only after profiling has identified an actual bottleneck.

## Pre-Merge Checklist

Before merging, ask:

- Is the code readable?
- Can names be improved?
- Can duplication be removed?
- Can nesting be reduced?
- Is error handling complete?
- Is it tested?
- Is dead code removed?
- Is there unnecessary abstraction?

Delete unused:

- Variables
- Imports
- Functions
- Comments
- Feature flags

## Complexity Limits

Prefer:

- Under 30 lines per function
- Under 200 lines per file
- Fewer than 4 parameters
- Fewer than 3 levels of nesting

## Dependency Rule

Avoid introducing new dependencies unless they provide significant value. Prefer the standard library when practical.

## Security Rule

Never trust external input. Always validate, sanitize, and escape as appropriate.

Never expose internal errors directly to users.
