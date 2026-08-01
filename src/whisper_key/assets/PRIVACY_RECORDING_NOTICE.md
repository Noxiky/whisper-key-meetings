# WhisperKey — aviso de privacidad y grabación

Última revisión: 2026-07-17

## Antes de grabar

WhisperKey es una herramienta de captura. La persona que inicia una grabación es responsable de
informar a quienes participan y de obtener el consentimiento exigido por las reglas del lugar,
la organización y la ley aplicable. No uses WhisperKey para grabar conversaciones cuando no
tengas autorización.

La aplicación mantiene visible el estado de grabación mediante la ventana, la bandeja y el mini
control. Ocultar o minimizar la ventana no convierte una grabación en invisible.

## Qué se procesa y dónde

- La transcripción principal se ejecuta localmente con el modelo instalado.
- WhisperKey no necesita una API de transcripción pagada para sus funciones principales.
- No se envían automáticamente audio, transcripciones, capturas ni sesiones a Codex, Claude u
  otro servicio.
- Un archivo `handoff.md` solo contiene instrucciones locales. Compartir o procesar el paquete
  con otra herramienta siempre requiere una acción explícita del usuario.
- Las capturas de pantalla solo se realizan cuando el usuario pulsa una acción de captura.

## Retención

La configuración predeterminada conserva el audio de todas las fuentes habilitadas. Las sesiones,
dictados, transcripciones y adjuntos se guardan en la biblioteca local del perfil de Windows.
Cuando una política permita retirar audio, WhisperKey muestra primero una vista previa exacta y
usa una papelera recuperable propia. El texto literal no se reemplaza por el documento limpio.

## Registros y diagnóstico

El texto transcrito no se escribe en los registros por defecto. El paquete de diagnóstico creado
desde Ajustes se genera localmente y excluye mensajes de log sin procesar, archivos de ajustes,
transcripciones, audio, capturas, sesiones e historial de dictados. Los nombres de dispositivos se
reemplazan por referencias unidireccionales. Revisa `diagnostics.json` antes de compartir el ZIP.

## English summary

WhisperKey processes primary transcription locally and does not automatically upload audio,
transcripts, screenshots, or sessions. The person starting a recording must provide notice and
obtain any consent required by applicable rules and law. Audio is retained by default. The local
diagnostics ZIP excludes captured content, raw log messages, settings files, and device names.
