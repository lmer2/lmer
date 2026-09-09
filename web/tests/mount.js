import { mount } from '@vue/test-utils'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'

export function mountPane(component, props, options = {}) {
  return mount(component, {
    props,
    attachTo: document.body,
    global: { ...options.global, plugins: [createVuetify({ components, directives })] },
  })
}

export function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
