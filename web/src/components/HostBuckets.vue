<script setup>
// Unnamed repos awaiting adoption (#374): the validator's `tree.host-bucket`
// findings, one per bucket — work an unregistered repo wrote under its host
// directory because it had no short name yet (D9).
//
// A read view and never an adoption UI (§13.C). Adopting is one commit that
// chooses a short name, and the short name is the operator's choice: the line is
// handed over with a `<short-name>` placeholder in it, never a derived name, and
// copied rather than run. There is deliberately no button here that does
// anything but copy.
import CopyButton from './CopyButton.vue'

defineProps({
  buckets: { type: Array, default: () => [] },
})
</script>

<template>
  <div class="host-buckets">
    <div class="section-title">Unnamed repos awaiting adoption</div>
    <v-card class="mb-3">
      <v-card-text>
        <!-- The empty case says what the panel is for, so it does not read as
             a broken one. -->
        <p v-if="!buckets.length" class="text-medium-emphasis">
          No unnamed repo is waiting. Work from a repository the registry does not
          name lands in a host bucket, and is listed here until it is adopted.
        </p>
        <div
          v-for="bucket in buckets"
          :key="bucket.path"
          class="bucket-row py-2"
        >
          <code class="scroll-x">{{ bucket.path }}</code>
          <div class="d-flex align-center ga-2 mt-1">
            <code class="scroll-x text-medium-emphasis">{{ bucket.adopt_command }}</code>
            <CopyButton :text="bucket.adopt_command" label="Copy adopt command" />
          </div>
        </div>
        <p v-if="buckets.length" class="text-body-small text-medium-emphasis mt-2">
          Replace <code>&lt;short-name&gt;</code> with the name the project should
          have before running the line.
        </p>
      </v-card-text>
    </v-card>
  </div>
</template>
