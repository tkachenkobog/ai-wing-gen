'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  // environment
  detectPython: () => ipcRenderer.invoke('python:detect'),
  pathsInfo: () => ipcRenderer.invoke('paths:info'),

  // studies (project -> study)
  listStudies: () => ipcRenderer.invoke('studies:list'),
  currentStudy: () => ipcRenderer.invoke('studies:current'),
  selectStudy: (ref) => ipcRenderer.invoke('studies:select', ref),
  createProject: (p) => ipcRenderer.invoke('studies:createProject', p),
  createStudy: (p) => ipcRenderer.invoke('studies:createStudy', p),
  cloneStudy: (p) => ipcRenderer.invoke('studies:clone', p),
  renameStudy: (p) => ipcRenderer.invoke('studies:rename', p),
  deleteStudy: (p) => ipcRenderer.invoke('studies:delete', p),
  readStudyLog: (ref) => ipcRenderer.invoke('studies:readLog', ref || {}),
  renameProject: (p) => ipcRenderer.invoke('studies:renameProject', p),
  deleteProject: (p) => ipcRenderer.invoke('studies:deleteProject', p),
  openStudyFolder: (p) => ipcRenderer.invoke('studies:openFolder', p || {}),
  compareStudies: (list) => ipcRenderer.invoke('studies:compare', list),

  // schema + config
  loadSchema: () => ipcRenderer.invoke('schema:load'),
  regenSchema: () => ipcRenderer.invoke('schema:regen'),
  loadConfig: () => ipcRenderer.invoke('config:load'),
  saveConfig: (cfg) => ipcRenderer.invoke('config:save', cfg),
  saveConfigAs: (cfg) => ipcRenderer.invoke('config:saveAs', cfg),
  openConfigFrom: () => ipcRenderer.invoke('config:openFrom'),

  // sizing (requirements -> design point)
  loadRequirements: () => ipcRenderer.invoke('sizing:load'),
  saveRequirements: (r) => ipcRenderer.invoke('sizing:save', r),
  runSizing: (opts) => ipcRenderer.invoke('sizing:run', opts || {}),

  // run control
  runStart: () => ipcRenderer.invoke('run:start'),
  runStop: () => ipcRenderer.invoke('run:stop'),
  runStatus: () => ipcRenderer.invoke('run:status'),
  onLog: (cb) => ipcRenderer.on('run:log', (_e, p) => cb(p)),
  onRunState: (cb) => ipcRenderer.on('run:state', (_e, p) => cb(p)),

  // results
  listResults: () => ipcRenderer.invoke('results:list'),
  resultDetail: (dir) => ipcRenderer.invoke('results:detail', dir),
  clearResults: () => ipcRenderer.invoke('results:clear'),
  regenerate: (opts) => ipcRenderer.invoke('results:regenerate', opts || {}),

  // shell
  openPath: (p) => ipcRenderer.invoke('shell:open', p),
  reveal: (p) => ipcRenderer.invoke('shell:reveal', p),
});
