// The specs `import()` the application's own ES modules by their served URL from
// inside `page.evaluate`, where the browser resolves them against the app origin.
// tsc resolves against the filesystem and finds nothing, so it needs to be told
// these are real modules. Deliberately untyped: the app ships no declarations, and
// inventing signatures here would let a test type-check against a contract the
// module no longer has.
declare module "/static/js/app/*.js";
