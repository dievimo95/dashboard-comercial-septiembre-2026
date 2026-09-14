# Dashboard comercial en Streamlit

## Ejecutar localmente

```bash
pip install -r requirements.txt
streamlit run app.py
```

La aplicación abre con el corte actual de septiembre. Desde la barra lateral se pueden cargar un forecast, un archivo de stock y un PDF de facturas nuevos.

Después de validar y actualizar, el corte procesado se guarda comprimido en el almacenamiento local de ese navegador. Al refrescar, se recupera automáticamente. «Iniciar nuevo mes» borra esa copia. Los archivos originales no se publican ni se guardan en GitHub; conserve sus Excel y PDF como respaldo. Si borra los datos del navegador o cambia de equipo/navegador, deberá cargarlos de nuevo.

## Publicar en Streamlit Community Cloud

Suba esta carpeta a un repositorio de GitHub y seleccione `app.py` como archivo principal en Streamlit Community Cloud.
