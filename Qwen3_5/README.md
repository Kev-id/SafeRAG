1，	启动命令行推理：
cd /data/LLM-TPU/models/Qwen3_5/python_demo
python3 pipeline_text.py -m ../models/qwen3.5-4b-int4-autoround_w4bf16_seq2048_bm1688_2core_dynamic_20260416_145112.bmodel -c ../config
2，	启动fastapi服务:
cd /data/LLM-TPU/models/Qwen3_5/python_demo
3，	curl：

linux：
curl http://localhost:8000/v1/chat/completions   -H "Content-Type: application/json"   -d '{"messages": [{"role": "user", "content": "你好"}]}'

windows：
curl http://192.168.10.101:8000/v1/chat/completions -H "Content-Type: application/json" -d "{\"messages\": [{\"role\": \"user\", \"content\": \"你好\"}]}"
