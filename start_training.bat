@echo off
echo 🚀 Inizializzazione del Collector (C1)...
kathara exec c1 -- bash -c "cd / && dd if=/dev/zero of=dati_training.bin bs=1M count=10 2>/dev/null && python3 -m http.server 5000 >/dev/null 2>&1 &"

echo ⏳ Attendo 2 secondi per far stabilizzare il server...
timeout /t 2 /nobreak > NUL

echo 🔥 Avvio i Worker (W1, W2, W3, W4)...
kathara exec w1 -- bash -c "while true; do wget -q http://10.0.0.100:5000/dati_training.bin -O /dev/null; sleep 3; done &"
kathara exec w2 -- bash -c "while true; do wget -q http://10.0.0.100:5000/dati_training.bin -O /dev/null; sleep 3; done &"
kathara exec w3 -- bash -c "while true; do wget -q http://10.0.0.100:5000/dati_training.bin -O /dev/null; sleep 3; done &"
kathara exec w4 -- bash -c "while true; do wget -q http://10.0.0.100:5000/dati_training.bin -O /dev/null; sleep 3; done &"

echo ✅ Traffico di training avviato con successo! Guarda il terminale di POX.