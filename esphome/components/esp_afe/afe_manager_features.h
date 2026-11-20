#pragma once

#include "esp_afe.h"

#if defined(USE_ESP32) && defined(USE_ESP_AFE_GMF_PATH) && !defined(CLANG_TIDY)
namespace esphome::esp_afe {

// Keep an unchanged live AEC out of the vendor enable/disable callbacks.
inline int apply_aec_intent(esp_gmf_afe_manager_handle_t manager, bool enabled) {
  esp_gmf_afe_manager_features_t features{};
  const int ret = esp_gmf_afe_manager_get_features(manager, &features);
  if (ret != ESP_GMF_ERR_OK) return ret < 0 ? ret : ESP_GMF_ERR_FAIL;
  if (features.aec == enabled) return static_cast<int>(enabled);
  return esp_gmf_afe_manager_enable_features(manager, ESP_AFE_FEATURE_AEC, enabled);
}

// Called only by the existing config owner. GMF resets VAD on every feature
// call, so an unchanged intent must not be reapplied as a reset operation.
inline esp_gmf_err_t apply_vad_intent(esp_gmf_afe_manager_handle_t manager, bool enabled) {
  esp_gmf_afe_manager_features_t features{};
  auto ret = esp_gmf_afe_manager_get_features(manager, &features);
  if (ret != ESP_GMF_ERR_OK) return ret < 0 ? ret : ESP_GMF_ERR_FAIL;
  if (features.vad == enabled) return ESP_GMF_ERR_OK;

  ret = esp_gmf_afe_manager_enable_features(manager, ESP_AFE_FEATURE_VAD, enabled);
  if (ret < 0) return ret;
  // GMF returns reset_vad's result, which can hide a failed feature toggle.
  // Its feature cache records the enable/disable acknowledgement separately.
  ret = esp_gmf_afe_manager_get_features(manager, &features);
  if (ret != ESP_GMF_ERR_OK) return ret < 0 ? ret : ESP_GMF_ERR_FAIL;
  return features.vad == enabled ? ESP_GMF_ERR_OK : ESP_GMF_ERR_FAIL;
}

}  // namespace esphome::esp_afe
#endif
