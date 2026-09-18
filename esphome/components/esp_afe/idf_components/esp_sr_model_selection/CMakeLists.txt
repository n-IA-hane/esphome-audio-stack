# ESP-SR's prebuilt model dispatcher retains NSNet3's writable model weights
# even when Kconfig selects WebRTC. Do not link neural models in that profile.
# Explicit neural model selections retain the vendor dispatcher unchanged.
set(model_gate_sources "")
if(CONFIG_SR_NSN_WEBRTC)
  configure_file("${CMAKE_CURRENT_LIST_DIR}/neural_model_gate.c.in"
                 "${CMAKE_CURRENT_BINARY_DIR}/neural_model_gate.c" COPYONLY)
  list(APPEND model_gate_sources "${CMAKE_CURRENT_BINARY_DIR}/neural_model_gate.c")
endif()
idf_component_register(SRCS ${model_gate_sources} REQUIRES espressif__esp-sr)
if(CONFIG_SR_NSN_WEBRTC)
  # ESP-SR's prebuilt archives are linked after component archives. Retain
  # this one hook before those archives introduce the wrapped reference.
  target_link_options(${COMPONENT_LIB} INTERFACE
                      "-Wl,--wrap=esp_nsnet_handle_from_name"
                      "-Wl,--undefined=__wrap_esp_nsnet_handle_from_name")
endif()
