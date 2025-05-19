import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
from emisoras_data import PREDEFINED_STATIONS
import asyncio

# Cargar variables de entorno
load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
PREFIX = os.getenv('PREFIX', '!!')

DEDICATED_TEXT_CHANNEL_ID = os.getenv('DEDICATED_TEXT_ID')
RADIO_CONTROLS_MESSAGE_ID = os.getenv('RADIO_CONTROLS_ID')

# Configuración de Intents
intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# Almacenamiento global simple para el mensaje de controles y estado de radio
# Para múltiples servidores, esto necesitaría ser un diccionario por guild_id
controls_message_info = {"message_obj": None, "current_station_name": "Ninguna", "voice_channel_name": "Desconectado"}

# --- Función para Actualizar el Mensaje de Controles ---
async def update_controls_message(guild: discord.Guild, error_message: str = None):
    global controls_message_info
    if not DEDICATED_TEXT_CHANNEL_ID or not controls_message_info["message_obj"]:
        # Intentar buscar el mensaje si el objeto no está cargado (ej. después de un reinicio antes de on_ready completo)
        if DEDICATED_TEXT_CHANNEL_ID and RADIO_CONTROLS_MESSAGE_ID:
            try:
                text_channel = guild.get_channel(int(DEDICATED_TEXT_CHANNEL_ID))
                controls_message_info["message_obj"] = await text_channel.fetch_message(int(RADIO_CONTROLS_MESSAGE_ID))
            except:
                print("No se pudo encontrar el mensaje de controles para actualizarlo.")
                return # No se puede actualizar si no hay mensaje
        else:
            return


    # Actualizar estado de conexión de voz
    vc = guild.voice_client
    if vc and vc.is_connected():
        controls_message_info["voice_channel_name"] = vc.channel.name
    else:
        controls_message_info["voice_channel_name"] = "Desconectado 🚫"
        controls_message_info["current_station_name"] = "Ninguna" # Si no está en voz, no suena nada

    embed_color = discord.Color.gold()
    if controls_message_info["current_station_name"] != "Ninguna" and controls_message_info["voice_channel_name"] != "Desconectado 🚫":
        embed_color = discord.Color.green()
    elif controls_message_info["voice_channel_name"] == "Desconectado 🚫":
        embed_color = discord.Color.red()


    embed = discord.Embed(
        title="📻 Panel de Control de Rock & Bot 🤘",
        description="Usa los controles de abajo para manejar la radio.",
        color=embed_color
    )
    embed.add_field(name="🔊 Estado Conexión de Voz", value=f"`{controls_message_info['voice_channel_name']}`", inline=True)
    embed.add_field(name="🎶 Actualmente Sonando", value=f"`{controls_message_info['current_station_name']}`", inline=True)

    if error_message:
        embed.add_field(name="⚠️ Último Error", value=error_message, inline=False)
        embed.color = discord.Color.orange() # Cambiar color si hay error

    embed.set_footer(text=f"Bot {bot.user.name} | {PREFIX}help")
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/2907/2907109.png") # Ejemplo de Thumbnail

    view = PersistentRadioControlsView(PREDEFINED_STATIONS) # Siempre reenviar la vista para asegurar que esté activa

    try:
        if controls_message_info["message_obj"]:
            await controls_message_info["message_obj"].edit(content=None, embed=embed, view=view)
    except discord.NotFound:
        print("El mensaje de controles original fue borrado. Se intentará recrear en la próxima ejecución de on_ready si RADIO_CONTROLS_MESSAGE_ID está vacío.")
        controls_message_info["message_obj"] = None # Marcar como no encontrado
    except Exception as e:
        print(f"Error al editar el mensaje de controles: {e}")

# --- Función Auxiliar para Reproducir Audio (modificada para actualizar panel) ---
async def _play_station_logic(interaction_or_ctx, station_key_or_url: str):
    global controls_message_info
    is_interaction = isinstance(interaction_or_ctx, discord.Interaction)

    user = interaction_or_ctx.user if is_interaction else interaction_or_ctx.author
    guild = interaction_or_ctx.guild
    voice_client = guild.voice_client

    error_to_display_on_panel = None

    if not voice_client or not voice_client.is_connected():
        error_to_display_on_panel = f"No estoy en un canal de voz. Usa el botón 'Conectarse'."
        if is_interaction:
            # El defer ya se hizo, ahora usamos followup para el mensaje efímero
            await interaction_or_ctx.followup.send(error_to_display_on_panel, ephemeral=True)
        else:
            await interaction_or_ctx.send(error_to_display_on_panel)
        await update_controls_message(guild, error_message=error_to_display_on_panel)
        return

    if user.voice is None or user.voice.channel != voice_client.channel:
        error_to_display_on_panel = f"Debes estar en mi mismo canal ({voice_client.channel.mention}) para cambiar la emisora."
        if is_interaction: await interaction_or_ctx.followup.send(error_to_display_on_panel, ephemeral=True)
        else: await interaction_or_ctx.send(error_to_display_on_panel)
        await update_controls_message(guild, error_message=error_to_display_on_panel)
        return

    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()
        await asyncio.sleep(0.5)

    actual_stream_url = ""
    station_display_name_for_panel = "Desconocida"
    input_key = station_key_or_url.lower().strip()

    if input_key in PREDEFINED_STATIONS:
        station_data = PREDEFINED_STATIONS[input_key]
        actual_stream_url = station_data["url"]
        station_display_name_for_panel = station_data["name"]
    else:
        actual_stream_url = station_key_or_url.strip("<>")
        station_display_name_for_panel = "URL Directa" # O podrías intentar obtener un título si es un stream con metadata

    if not actual_stream_url:
        error_to_display_on_panel = f"No pude determinar una URL para: `{station_key_or_url}`."
        if is_interaction: await interaction_or_ctx.followup.send(error_to_display_on_panel, ephemeral=True)
        else: await interaction_or_ctx.send(error_to_display_on_panel)
        await update_controls_message(guild, error_message=error_to_display_on_panel)
        return

    ffmpeg_options = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin',
        'options': '-vn'
    }

    try:
        audio_source = discord.FFmpegPCMAudio(actual_stream_url, **ffmpeg_options)
        if not voice_client.is_playing():
            voice_client.play(audio_source, after=lambda e: asyncio.run_coroutine_threadsafe(after_playback_error_handler(guild, e, station_display_name_for_panel), bot.loop))

            controls_message_info["current_station_name"] = station_display_name_for_panel
            if is_interaction: # El mensaje efímero de defer ya se envió. Solo actualizamos panel.
                 await interaction_or_ctx.followup.send(f"✅ Sintonizando: **{station_display_name_for_panel}**",ephemeral=True)
            else: # Para comando !play
                await interaction_or_ctx.send(f"🎧 ¡Reproduciendo ahora: **{station_display_name_for_panel}** en {voice_client.channel.mention}!")

            await update_controls_message(guild) # Actualiza el panel con la nueva emisora

    except Exception as e:
        print(f"Critical error playing ({station_display_name_for_panel}): {e}")
        error_message_str = str(e)
        error_to_display_on_panel = f"No pude reproducir **{station_display_name_for_panel}**. Error: `{error_message_str}`"
        if is_interaction: await interaction_or_ctx.followup.send(error_to_display_on_panel, ephemeral=True)
        else: await interaction_or_ctx.send(error_to_display_on_panel)
        controls_message_info["current_station_name"] = "Error al reproducir"
        await update_controls_message(guild, error_message=error_to_display_on_panel)

async def after_playback_error_handler(guild: discord.Guild, error, station_name: str):
    if error:
        print(f'Error del reproductor para {station_name} en guild {guild.id}: {error}')
        controls_message_info["current_station_name"] = f"Error en {station_name}"
    else: # Reproducción terminó normalmente (o fue detenida)
        # No necesariamente significa que debamos poner "Ninguna", podría haber sido detenida para cambiar.
        # La lógica de stop/play se encarga de poner la nueva.
        # Si un stream termina por sí solo (muy raro para radio), entonces sí.
        print(f"Reproducción de {station_name} finalizada en guild {guild.id}.")
        # Si queremos que al terminar un stream se ponga "Ninguna"
        # if guild.voice_client and not guild.voice_client.is_playing():
        # controls_message_info["current_station_name"] = "Ninguna"
    await update_controls_message(guild, error_message=str(error) if error else None)


# --- Clases para la Vista de Controles Persistentes ---
class JoinVoiceButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Conectarme a Voz", style=discord.ButtonStyle.green, custom_id="persistent_join_voice_button", emoji="🎤")

    async def callback(self, interaction: discord.Interaction):
        global controls_message_info
        user = interaction.user
        guild = interaction.guild
        voice_client = guild.voice_client
        error_to_display = None

        if not user.voice:
            await interaction.response.send_message("⚠️ Debes estar en un canal de voz para que pueda unirme.", ephemeral=True)
            return

        user_voice_channel = user.voice.channel

        await interaction.response.defer(ephemeral=True, thinking=True) # Deferir porque conectar puede tardar

        if voice_client is None:
            try:
                await user_voice_channel.connect()
                controls_message_info["voice_channel_name"] = user_voice_channel.name
                await interaction.followup.send(f"✅ ¡Conectado a **{user_voice_channel.name}**! Ahora puedes seleccionar una emisora.", ephemeral=True)
            except Exception as e:
                error_to_display = f"🛑 No pude unirme a tu canal: {e}"
                await interaction.followup.send(error_to_display, ephemeral=True)
        elif voice_client.channel == user_voice_channel:
            controls_message_info["voice_channel_name"] = user_voice_channel.name # Asegurar que esté actualizado
            await interaction.followup.send(f"👍 Ya estoy en tu canal: **{user_voice_channel.name}**.", ephemeral=True)
        else:
            try:
                await voice_client.move_to(user_voice_channel)
                controls_message_info["voice_channel_name"] = user_voice_channel.name
                await interaction.followup.send(f"✅ Me he movido a tu canal: **{user_voice_channel.name}**.", ephemeral=True)
            except Exception as e:
                error_to_display = f"🛑 No pude moverme a tu canal: {e}"
                await interaction.followup.send(error_to_display, ephemeral=True)

        await update_controls_message(guild, error_message=error_to_display)

class StopAndLeaveButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Detener y Salir", style=discord.ButtonStyle.red, custom_id="persistent_stop_leave_button", emoji="✖️")

    async def callback(self, interaction: discord.Interaction):
        global controls_message_info
        guild = interaction.guild
        voice_client = guild.voice_client

        await interaction.response.defer(ephemeral=True, thinking=True)

        if voice_client and voice_client.is_connected():
            if voice_client.is_playing():
                voice_client.stop()
            await voice_client.disconnect()
            controls_message_info["current_station_name"] = "Ninguna"
            controls_message_info["voice_channel_name"] = "Desconectado 🚫"
            await interaction.followup.send("👋 Radio detenida y me he desconectado.", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ No estoy conectado a ningún canal de voz.", ephemeral=True)

        await update_controls_message(guild)


class StationSelect(discord.ui.Select):
    def __init__(self, options_list, placeholder_text):
        super().__init__(custom_id="persistent_station_select_menu", placeholder=placeholder_text, min_values=1, max_values=1, options=options_list)

    async def callback(self, interaction: discord.Interaction):
        global controls_message_info
        selected_station_key = self.values[0]

        if not interaction.guild.voice_client or not interaction.guild.voice_client.is_connected():
            await interaction.response.send_message("⚠️ Primero debo estar en un canal de voz. Usa el botón 'Conectarme'.", ephemeral=True)
            return
        if interaction.user.voice is None or interaction.user.voice.channel != interaction.guild.voice_client.channel:
            await interaction.response.send_message(f"⚠️ Debes estar en mi mismo canal de voz ({interaction.guild.voice_client.channel.mention}) para cambiar la emisora.", ephemeral=True)
            return

        station_name_for_feedback = PREDEFINED_STATIONS.get(selected_station_key, {}).get("name", selected_station_key)
        await interaction.response.defer(ephemeral=True, thinking=True) # Deferir la respuesta efímera

        await _play_station_logic(interaction, selected_station_key)
        # La actualización del panel y el mensaje de "Reproduciendo ahora" (no efímero) se manejan en _play_station_logic

class PersistentRadioControlsView(discord.ui.View):
    def __init__(self, stations_dict):
        super().__init__(timeout=None)

        self.add_item(JoinVoiceButton())
        self.add_item(StopAndLeaveButton()) # Nuevo botón para detener y salir

        options = []
        for i, (key, station_info) in enumerate(stations_dict.items()):
            if i >= 25: break
            options.append(discord.SelectOption(
                label=station_info['name'][:100], value=key, emoji="🎶", # Ejemplo de emoji
                description=f"Escuchar {station_info['name'][:100]}"
            ))

        if options:
            self.add_item(StationSelect(options_list=options, placeholder_text="🎶 Elige una emisora..."))

# --- Eventos del Bot ---
@bot.event
async def on_ready():
    global controls_message_info
    print(f'¡Bot {bot.user.name} está en línea y listo!')
    print(f'Prefijo de comandos: {PREFIX}')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="la radio | "+PREFIX+"help"))

    # Es importante registrar la vista ANTES de intentar interactuar con mensajes antiguos
    bot.add_view(PersistentRadioControlsView(PREDEFINED_STATIONS))
    print("Vista persistente de controles de radio registrada.")

    if DEDICATED_TEXT_CHANNEL_ID:
        try:
            text_channel_id = int(DEDICATED_TEXT_CHANNEL_ID)
            # Asumimos que el bot solo necesita operar en un guild principal para este panel
            # Si tienes múltiples guilds, necesitarías una forma de identificar el guild correcto
            # o tener paneles por guild. Aquí tomamos el primer guild para simplicidad.
            target_guild = bot.guilds[0] if bot.guilds else None
            if not target_guild:
                print("El bot no está en ningún servidor. Panel no configurado.")
                return

            text_channel = target_guild.get_channel(text_channel_id)

            if text_channel and isinstance(text_channel, discord.TextChannel):
                print(f"Buscando/Actualizando mensaje de controles en: {text_channel.name}")

                initial_message_id = None
                if RADIO_CONTROLS_MESSAGE_ID:
                    try:
                        initial_message_id = int(RADIO_CONTROLS_MESSAGE_ID)
                        controls_message_info["message_obj"] = await text_channel.fetch_message(initial_message_id)
                    except (discord.NotFound, ValueError):
                        print(f"ID de mensaje ({RADIO_CONTROLS_MESSAGE_ID}) no válido o mensaje no encontrado. Se creará uno nuevo.")
                        initial_message_id = None # Forzar creación
                        controls_message_info["message_obj"] = None # Resetear

                if not controls_message_info["message_obj"]: # Si no se pudo cargar o no había ID
                    # Limpiar mensajes antiguos del bot en el canal (con cuidado si hay otros mensajes del bot)
                    # async for message in text_channel.history(limit=10):
                    # if message.author == bot.user and not (message.components and any(c.custom_id == "persistent_join_voice_button" for c in message.components[0].children)): # Evitar borrar el panel actual si ya existe
                    # await message.delete()
                    # print("Mensajes antiguos del bot limpiados (si los había).")

                    view = PersistentRadioControlsView(PREDEFINED_STATIONS) # Crear la vista para el nuevo mensaje
                    embed = discord.Embed(title="Cargando Panel de Radio...", color=discord.Color.light_grey())
                    controls_message_info["message_obj"] = await text_channel.send(content="📡", embed=embed, view=view)
                    print(f"NUEVO MENSAJE DE CONTROLES ENVIADO. Su ID es: {controls_message_info['message_obj'].id}")
                    print("POR FAVOR, ACTUALIZA 'RADIO_CONTROLS_MESSAGE_ID' EN TU ARCHIVO .ENV CON ESTE NUEVO ID.")

                await update_controls_message(target_guild) # Llamada inicial para establecer el estado correcto

            else:
                print(f"ID de canal de texto ({DEDICATED_TEXT_CHANNEL_ID}) no encontrado o no es un canal de texto.")
        except ValueError:
            print("Error: DEDICATED_TEXT_CHANNEL_ID en .env debe ser un número entero.")
        except Exception as e:
            print(f"Error catastrófico durante on_ready en config. de panel: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("DEDICATED_TEXT_CHANNEL_ID no configurado. El panel de control persistente no se activará.")

# --- Listener para Voice State Updates (opcional, para actualizar panel si el bot es desconectado) ---
@bot.event
async def on_voice_state_update(member, before, after):
    global controls_message_info
    # Si el miembro que cambió de estado es nuestro bot
    if member.id == bot.user.id:
        if before.channel and not after.channel: # El bot fue desconectado de un canal
            print(f"Bot desconectado del canal de voz {before.channel.name} en {member.guild.name}")
            controls_message_info["voice_channel_name"] = "Desconectado 🚫"
            controls_message_info["current_station_name"] = "Ninguna"
            await update_controls_message(member.guild)
        elif not before.channel and after.channel: # El bot se conectó a un canal
            print(f"Bot conectado al canal de voz {after.channel.name} en {member.guild.name}")
            controls_message_info["voice_channel_name"] = after.channel.name
            # No cambiamos current_station_name aquí, eso lo hace la lógica de play
            await update_controls_message(member.guild)
        elif before.channel != after.channel and after.channel: # El bot se movió a otro canal
            print(f"Bot movido de {before.channel.name} a {after.channel.name} en {member.guild.name}")
            controls_message_info["voice_channel_name"] = after.channel.name
            await update_controls_message(member.guild)


# --- Comandos del Bot (simplificados o mantenidos para flexibilidad) ---
@bot.command(name='join', aliases=['conectar', 'j'], help='El bot se une al canal de voz del usuario.')
async def join(ctx):
    # Este comando ahora es más un alias del botón o para casos específicos
    user = ctx.author
    guild = ctx.guild
    voice_client = guild.voice_client

    if not user.voice:
        await ctx.send(f"⚠️ {user.mention}, ¡no estás conectado a un canal de voz!")
        return

    user_voice_channel = user.voice.channel

    if voice_client is None:
        try:
            await user_voice_channel.connect()
            await ctx.send(f"🎤 ¡Conectado a **{user_voice_channel.name}**!")
            await update_controls_message(guild)
        except Exception as e:
            await ctx.send(f"🛑 No pude unirme a tu canal: {e}")
    elif voice_client.channel == user_voice_channel:
        await ctx.send(f"👍 Ya estoy en tu canal: **{user_voice_channel.name}**.")
    else:
        try:
            await voice_client.move_to(user_voice_channel)
            await ctx.send(f"🎤 Me he movido a tu canal: **{user_voice_channel.name}**.")
            await update_controls_message(guild)
        except Exception as e:
            await ctx.send(f"🛑 No pude moverme a tu canal: {e}")


@bot.command(name='leave', aliases=['disconnect', 'salir', 'l'], help='El bot abandona el canal de voz.')
async def leave(ctx):
    # Este comando ahora también actualiza el panel
    guild = ctx.guild
    voice_client = guild.voice_client
    if voice_client and voice_client.is_connected():
        if voice_client.is_playing():
            voice_client.stop()
        await voice_client.disconnect()
        await ctx.send("👋 Chao pescao.")
        # El evento on_voice_state_update se encargará de actualizar el panel
    else:
        await ctx.send("⚠️ No estoy en ningún canal de voz, compa.")


@bot.command(name='play', aliases=['p'], help=f'Reproduce una emisora. Uso: {PREFIX}play [nombre_clave | URL]')
async def play(ctx, *, station_input: str):
    # Este comando sigue siendo útil
    await _play_station_logic(ctx, station_input)

# El comando `emisoras` puede ser útil para debug o si alguien borra el panel
@bot.command(name="panelradio", help="(Re)envía el panel de control de la radio al canal dedicado.")
@commands.has_permissions(manage_guild=True) # Solo admins pueden reenviar el panel
async def panelradio(ctx):
    global controls_message_info
    if DEDICATED_TEXT_CHANNEL_ID:
        text_channel_id = int(DEDICATED_TEXT_CHANNEL_ID)
        text_channel = ctx.guild.get_channel(text_channel_id)
        if text_channel:
            # Borrar el mensaje antiguo si tenemos su ID y existe
            if RADIO_CONTROLS_MESSAGE_ID and controls_message_info["message_obj"]:
                try:
                    old_msg = await text_channel.fetch_message(int(RADIO_CONTROLS_MESSAGE_ID))
                    await old_msg.delete()
                    print(f"Mensaje de panel antiguo (ID: {RADIO_CONTROLS_MESSAGE_ID}) borrado.")
                except discord.NotFound:
                    print(f"Mensaje de panel antiguo (ID: {RADIO_CONTROLS_MESSAGE_ID}) no encontrado para borrar.")
                except Exception as e:
                    print(f"Error borrando mensaje de panel antiguo: {e}")

            view = PersistentRadioControlsView(PREDEFINED_STATIONS)
            embed = discord.Embed(title="Cargando Panel de Radio...", color=discord.Color.light_grey())
            new_panel_msg = await text_channel.send(content="📡", embed=embed, view=view)
            controls_message_info["message_obj"] = new_panel_msg
            await update_controls_message(ctx.guild) # Actualiza con el estado correcto
            await ctx.send(f"✅ Panel de radio reenviado. Nuevo ID de mensaje: `{new_panel_msg.id}`. **¡Actualiza tu .env!**", ephemeral=True)
            print(f"PANEL MANUALMENTE REENVIADO. Nuevo ID: {new_panel_msg.id}. ACTUALIZA .ENV")
        else:
            await ctx.send("❌ Canal de texto dedicado no encontrado.", ephemeral=True)
    else:
        await ctx.send("❌ No hay canal de texto dedicado configurado en .env.", ephemeral=True)


# --- Manejo de Errores de Comandos ---
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send(f"⚠️ Comando no encontrado. Usa `{PREFIX}help` noma po.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Te faltó algo pa el comando, revisa con `{PREFIX}help {ctx.command.name}`.")
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send("🚫 Este comando no se puede usar en mensajes privados.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send(f"🚫 Te faltan permisos para usar este comando: {', '.join(error.missing_perms)}")
    else:
        print(f'Error no manejado en el comando {ctx.command if ctx.command else "desconocido"}: {error}')
        # await ctx.send(f"🆘 Error brigido con el comando: {error}")


# --- Ejecutar el Bot ---
if __name__ == "__main__":
    if not TOKEN: print("Error: BOT_TOKEN no encontrado en .env.")
    # Ya no es crítico que DEDICATED_TEXT_CHANNEL_ID esté para arrancar, pero sí para el panel.

    if TOKEN:
        try:
            print("Intentando conectar el bot...")
            bot.run(TOKEN)
        except Exception as e:
            print(f"Ocurrió un error al intentar ejecutar el bot: {e}")
            if isinstance(e, discord.errors.LoginFailure): print("Verifica tu BOT_TOKEN.")
            if isinstance(e, discord.errors.PrivilegedIntentsRequired): print("Habilita 'MESSAGE CONTENT INTENT'.")
    else:
        print("El bot no puede iniciar sin un BOT_TOKEN.")
