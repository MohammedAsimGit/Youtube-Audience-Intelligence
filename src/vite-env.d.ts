// Compiled CSS is imported as a string (`?inline`) and injected into the
// Shadow DOM root at runtime - no separate CSS asset, no <link> to the page.
declare module '*.css?inline' {
  const css: string;
  export default css;
}
