import { flushPromises, mount } from '@vue/test-utils'
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

// The bar's project switcher (#547). Its menu renders in an overlay outside the
// wrapper, so the entry is found in the document.
export async function chooseProject(wrapper, name) {
  await wrapper.get('.project-switcher').trigger('click')
  await flushPromises()
  const item = [...document.querySelectorAll('.project-switcher-list .v-list-item')]
    .find(row => row.querySelector('.v-list-item-title')?.textContent.trim() === name)
  if (!item) throw new Error(`no ${name} entry in the project switcher`)
  item.click()
  await flushPromises()
}
