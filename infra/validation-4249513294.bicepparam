using 'main.bicep'

param deleteAfter = '2026-12-31'

param embedCapacity = 120

param embedModelName = 'text-embedding-3-large'

param embedModelVersion = '1'

param enableSemanticCache = false

param environmentName = 'atlas'

param gptJudgeCapacity = 100

param gptJudgeModelName = 'gpt-5.4-mini'

param gptJudgeModelVersion = '2026-03-17'

param gptMainCapacity = 200

param gptMainModelName = 'gpt-5.4'

param gptMainModelVersion = '2026-03-05'

param location = 'eastus2'

param opsEmail = 'passadikosta@gmail.com'

param owner = 'reactor-demo'

param principalId = '7e35709d-f693-4896-9599-146e27046ef4'

param principalType = 'User'
