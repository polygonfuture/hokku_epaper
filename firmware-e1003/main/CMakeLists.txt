idf_component_register(
    SRCS "main.c" "config.c" "text_render.c" "it8951.c" "charger_sy6974b.c"
    INCLUDE_DIRS "." "${CMAKE_BINARY_DIR}"
)
