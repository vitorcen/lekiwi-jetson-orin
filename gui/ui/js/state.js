// Shared helpers + the one cross-module mutable: which tab is showing.
export const invoke = window.__TAURI__ && window.__TAURI__.core
                    ? window.__TAURI__.core.invoke : null;
export const $ = id => document.getElementById(id);
export const S = { page: 'zmq' };
