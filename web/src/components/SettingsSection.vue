<script setup>
// One settings card (issue 652): title, one-line description, body, then the
// card's actions with its primary Save last. Save is enabled only while the card
// holds unsaved edits, so an untouched card offers nothing to press.
import { computed, useId } from 'vue'

const props = defineProps({
  title: { type: String, required: true },
  description: { type: String, default: '' },
  headingId: { type: String, default: null },
  dirty: Boolean,
  // A card with no form leaves this unset and renders no Save.
  saveLabel: { type: String, default: null },
  saving: Boolean,
  saveDisabled: Boolean,
  // Save without an edit: a stored value that only a save can repair.
  saveable: Boolean,
})
const emit = defineEmits(['save'])
const generated = useId()
const heading = computed(() => props.headingId || `settings-section-${generated}`)
</script>

<template>
  <v-card class="settings-section mb-4" tag="section" :aria-labelledby="heading">
    <v-card-item>
      <div class="settings-section-head">
        <h2 :id="heading" tabindex="-1" class="text-title-medium settings-section-title">{{ title }}</h2>
        <v-chip v-if="dirty" color="warning" variant="outlined" class="settings-unsaved">Unsaved</v-chip>
      </div>
      <p v-if="description" class="text-body-medium settings-section-description mt-1">{{ description }}</p>
    </v-card-item>
    <v-card-text><slot /></v-card-text>
    <v-card-actions v-if="$slots.actions || saveLabel" class="settings-section-actions">
      <slot name="actions" />
      <v-spacer />
      <v-btn v-if="saveLabel" color="primary" variant="flat" class="settings-save" :loading="saving"
        :disabled="!(dirty || saveable) || saveDisabled" @click="emit('save')">{{ saveLabel }}</v-btn>
    </v-card-actions>
  </v-card>
</template>

<style scoped>
.settings-section-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.settings-section-title { scroll-margin-top: calc(var(--v-layout-top, 64px) + 16px); }
.settings-section-actions { flex-wrap: wrap; gap: 8px; padding-inline: 16px; padding-bottom: 16px; }
.settings-section-actions > .v-btn { margin-inline-start: 0 !important; }
</style>
