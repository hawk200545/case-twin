/** The sole frontend configuration point for the CaseTwin backend. */
export const API_BASE = (import.meta.env.VITE_API_URL || "http://localhost:8005").replace(/\/$/, "");
