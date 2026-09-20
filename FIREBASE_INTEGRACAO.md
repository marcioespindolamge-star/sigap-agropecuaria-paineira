# SIGAP — Integração Firebase/Firestore

Branch de trabalho: `integracao-firebase-segura`.

## Projeto Firebase

- Project ID: `sigap-agropecuaria-paineira`
- Firestore criado.
- Configuração Web registrada em `static/firebase-config.js`.

## Segurança

O arquivo `firestore.rules` começa com leitura e gravação bloqueadas. Não colocar chave privada de conta de serviço no GitHub.

## Estratégia

A versão principal do SIGAP permanece intacta. A integração será feita nesta branch e validada antes de qualquer alteração na `main`.

A próxima etapa é ligar os cadastros do SIGAP ao Firestore preservando as regras atuais do sistema e os dados locais existentes.
