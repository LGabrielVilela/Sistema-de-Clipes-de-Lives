# Cortador de Lives

Ferramenta local que pega um link de vídeo ou de live (YouTube, Twitch, Kick e outros que o yt-dlp suporta), encontra os melhores momentos e entrega **cortes verticais 9:16 com legenda animada**, prontos para TikTok, Reels e YouTube Shorts.

## Como usar

1. Dê dois cliques em **`iniciar.bat`**. Na primeira vez ele instala tudo, inclusive o ffmpeg, e leva alguns minutos. A primeira transcrição ainda baixa o modelo de voz, de cerca de 1,6 GB.
2. O navegador abre sozinho no painel. Deixe a janela preta aberta enquanto usa.
3. Cole o link e escolha:
   - **Vídeo gravado**: baixa e corta. Para VODs longos, preencha "Só a partir do min." e "Até o min." para analisar apenas um trecho.
   - **Live agora**: grava a live em blocos (por exemplo, a cada 15 minutos). Cada bloco é cortado **enquanto a live continua**. Se o sinal cair, a gravação reconecta sozinha.
4. Revise os cortes. Dá para ajustar início, fim e título e renderizar de novo, baixar um por um ou **baixar todos em .zip**. Cada corte vem com um `.txt` com título e hashtags prontos para o post.

Os arquivos ficam em `data/jobs/<id>/clips/`.

## Robô de cortes automáticos (abas Streamers, Agenda e Contas)

1. **Contas**: cole a chave do Claude e conecte o YouTube e o TikTok, seguindo o passo a passo de cada cartão. As chaves da Twitch e da Kick são opcionais e servem para filtrar pela categoria exata do jogo.
2. **Streamers**: cadastre os canais **que autorizam cortes** (Twitch, Kick ou YouTube) e ligue o robô. A cada 3 minutos ele confere quem está ao vivo. Se a categoria ou o título bate com as palavras-chave (padrão: GTA 6), ele grava e corta em blocos enquanto a live continua. Reprises são ignoradas.
3. **Agenda**: os cortes com nota acima do mínimo chegam com título, descrição, tags e hashtags gerados pela IA a partir das tendências. Você revisa, edita se quiser e aprova, ou ligue **Aprovar sozinho** para que os cortes com nota alta (padrão: 75 ou mais) entrem direto na agenda. O robô posta nos próximos horários livres (padrão: 12h, 18h e 21h, por plataforma).
   - **YouTube**: o vídeo sobe na hora da aprovação, já agendado, e o próprio YouTube publica no horário, mesmo com o PC desligado.
   - **TikTok**: no horário, o vídeo vai para os **rascunhos** do app (você toca em publicar) ou é postado direto, depois que o app passar pela auditoria. Para isso o PC precisa estar ligado.
4. **Hashtags em alta**: o robô lê as hashtags e tags dos Shorts mais vistos das últimas 72 h sobre o jogo (2 vezes por dia). Para o TikTok, copie as hashtags em alta do [Creative Center](https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/pt) para o campo da Agenda, já que o TikTok não oferece API para isso.

**Limites das plataformas**:
- Até o Google e o TikTok auditarem seu app (processo gratuito pedido nos portais de desenvolvedor), os vídeos enviados pela API ficam privados. No TikTok, use o modo rascunho enquanto isso.
- A cota padrão do YouTube permite cerca de 6 envios por dia.
- O TikTok aceita no máximo 5 rascunhos pendentes por dia.

## Enquadramento

| Opção | Quando usar |
|---|---|
| **Automático** (padrão) | Detecta sozinho. Se a stream tem webcam num canto, monta a webcam em cima e o jogo embaixo. Se o rosto ocupa a tela (podcast, IRL), faz tela cheia seguindo o rosto. Se não acha rosto, usa o vídeo inteiro com fundo desfocado. |
| Webcam em cima + jogo embaixo | Força o formato clássico de cortes de stream de jogo. |
| Tela cheia seguindo o rosto | Podcasts, react e lives "só câmera". |
| Vídeo inteiro + fundo desfocado | Quando nada pode ser cortado das laterais. |

## IA (grátis por padrão)

O Cortador escolhe a IA sozinho, nesta ordem:

1. **Claude Haiku** (opcional, melhor qualidade): usado quando há uma chave do Claude com saldo, colada na aba Contas ou via `ant auth login`. Custa poucos centavos de dólar por live de 8 horas.
2. **IA local grátis (Ollama, modelo `qwen2.5:3b`)**: roda no seu próprio PC, sem custo e sem limite. O `iniciar.bat` instala e baixa o modelo (cerca de 2 GB) na primeira vez.
3. **Modo simples**: se nenhuma das duas estiver disponível, os cortes saem pelos picos de volume e de chat.

Se o Claude responder "sem créditos" ou "chave inválida", ele fica uma hora em pausa e a IA local assume sozinha. O topo do painel mostra qual IA está em uso.

## Como funciona

1. **yt-dlp** baixa o vídeo, ou o **ffmpeg** grava a live em blocos com reconexão automática.
2. **faster-whisper** (`large-v3-turbo`, na GPU NVIDIA) transcreve em blocos de 20 minutos, com o tempo de cada palavra.
3. Picos de **volume do áudio** e de **mensagens no chat** (replay de chat do YouTube) indicam os momentos quentes.
4. O **Claude** lê a transcrição e escolhe os trechos com gancho, início e fim naturais, título e hashtags.
5. O **OpenCV** detecta rosto e webcam, e o **ffmpeg** renderiza 1080x1920 com legenda palavra por palavra, usando a NVENC da placa quando ela está disponível.

## Botões úteis

- **Cancelar / Tentar de novo**: se algo falhar ou você fechar o programa no meio, "Tentar de novo" continua de onde parou, sem baixar nem transcrever de novo.
- **Gerar novos cortes**: refaz só a escolha e a renderização, por exemplo com outro número de cortes, outra duração ou outro enquadramento.

## Requisitos

- Windows 10/11 com Python 3.10 ou mais novo
- Placa NVIDIA recomendada. Sem ela, a transcrição roda na CPU com o modelo `small`, bem mais lenta.

## Avisos importantes

- **Direitos autorais**: só publique cortes de criadores que permitem isso. Muitos streamers têm programas oficiais de cortes; procure o regulamento de cada um. Cortes sem autorização podem ser derrubados e gerar strikes na sua conta.
- Revise cada corte antes de postar.
- O chat só é usado como sinal no YouTube (replay de chat). Na Twitch e no Kick, a escolha usa fala e volume.
