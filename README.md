# ICARUS

**Impact of Clustering Alternatives for Radio Unit Selection**

O ICARUS é um framework experimental para avaliar como diferentes estratégias de associação entre **O-RUs** (*O-RAN Radio Units*) e **O-DUs** (*O-RAN Distributed Units*) afetam o consumo de energia, a utilização de CPU e outros recursos computacionais de uma topologia Open RAN.

O projeto utiliza dados públicos disponibilizados pela **Agência Nacional de Telecomunicações (ANATEL)** para construir e executar dois cenários experimentais equivalentes quanto à quantidade de estações, carga total e infraestrutura disponível:

- **Cenário otimizado:** produzido por um modelo de Programação Linear Inteira — ILP.
- **Cenário adversarial:** construído deliberadamente a partir do cenário otimizado para aumentar o estresse da infraestrutura, por meio do desbalanceamento das cargas, das larguras de banda e das distâncias entre as O-RUs e as respectivas O-DUs.

Um pipeline automatizado configura os dois cenários, executa as emulações, coleta métricas e armazena os resultados em um banco de dados **SQLite**, permitindo a análise posterior do impacto das estratégias de posicionamento e clusterização sobre os recursos computacionais e energéticos.

## Objetivos

O ICARUS foi desenvolvido para:

- processar registros públicos de estações da ANATEL;
- representar estações como O-RUs e possíveis localizações de O-DUs;
- gerar dois cenários de associações O-RU–O-DU por meio de otimização ILP: uma otimizada e outra por estresse;
- configurar automaticamente O-DUs e emuladores de O-RU seguindo as associações geradas pelo ILP;
- aplicar atrasos de fronthaul proporcionais às distâncias geográficas;
- executar os cenários em condições controladas;
- coletar métricas de CPU, memória, energia e rede;
- armazenar os resultados em SQLite;
- apoiar análises comparativas entre as estratégias de posicionamento.

## Visão geral do fluxo

```text
Dados públicos da ANATEL
          |
          v
Filtragem e consolidação das estações
          |
          v
Matriz de distâncias e cargas das O-RUs
          |
          +---------------------------+
          |                           |
          v                           v
Cenário otimizado            Cenário estressado
          |                           |
          +-------------+-------------+
                        |
                        v
          Geração dos arquivos de topologia
                        |
                        v
       Configuração das O-DUs e emuladores de O-RU
                        |
                        v
          Execução automatizada dos experimentos
                        |
                        v
        Coleta de métricas e armazenamento SQLite
                        |
                        v
                Análise comparativa
```

## Ambiente de referência

O roteiro abaixo considera uma instalação limpa do:

- Ubuntu Server 24.04;
- kernel Ubuntu Real-time;
- OCUDU compilado sem DPDK;
- Python gerenciado com Poetry e pipx;
- Solver Gurobi;
- Intel oneAPI MKL;
- AOCL FFTZ;
- `tuned` com perfil de desempenho do srsRAN.

> Os comandos podem exigir adaptação conforme o hardware, a versão dos pacotes e a política administrativa do servidor.

## 1. Configuração inicial do sistema

Defina o fuso horário:

```bash
sudo timedatectl set-timezone America/Belem
```

Desabilite serviços que podem introduzir atividade em segundo plano durante os experimentos:

```bash
sudo systemctl disable --now fwupd.service
sudo systemctl disable --now udisks2.service
sudo systemctl disable --now upower.service
sudo systemctl disable --now ModemManager
sudo systemctl disable --now canonical-livepatchd
sudo systemctl disable --now snapd
sudo systemctl disable --now unattended-upgrades
sudo apt purge -y fwupd udisks2 upower
```

Atualize os repositórios:

```bash
sudo apt update
```

## 2. Instalação das dependências do sistema

```bash
sudo apt-get install -y \
    autoconf \
    automake \
    build-essential \
    ccache \
    cmake \
    gcc \
    g++ \
    git \
    gpg-agent \
    libdw-dev \
    libfftw3-dev \
    libgtest-dev \
    libmbedtls-dev \
    libnuma-dev \
    libomp-dev \
    libsctp-dev \
    libtool \
    libyaml-cpp-dev \
    libzmq3-dev \
    make \
    meson \
    ninja-build \
    pipx \
    pkg-config \
    python3-pip \
    python3-pyelftools \
    tar \
    tuned \
    wget
```

## 3. Instalação do Intel oneAPI MKL

Importe a chave do repositório:

```bash
wget -O- \
    https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB \
    | gpg --dearmor \
    | sudo tee /usr/share/keyrings/oneapi-archive-keyring.gpg > /dev/null
```

Adicione o repositório:

```bash
echo "deb [signed-by=/usr/share/keyrings/oneapi-archive-keyring.gpg] https://apt.repos.intel.com/oneapi all main" \
    | sudo tee /etc/apt/sources.list.d/oneAPI.list
```

Instale o MKL:

```bash
sudo apt update
sudo apt install -y intel-oneapi-mkl-devel
```

Adicione a biblioteca ao carregador dinâmico:

```bash
echo "/opt/intel/oneapi/mkl/2026.0/lib" \
    | sudo tee /etc/ld.so.conf.d/intel-oneapi.conf

sudo ldconfig
```

Habilite o ambiente Intel oneAPI no shell:

```bash
echo 'source /opt/intel/oneapi/setvars.sh' >> ~/.bashrc
source ~/.bashrc
```

> Caso a versão instalada seja diferente de `2026.0`, ajuste o caminho em `/opt/intel/oneapi/mkl/`.

## 4. Instalação do AOCL FFTZ

```bash
AOCL_FFTZ_VERSION="5.2"

cd /tmp

wget --no-check-certificate -O - \
    "https://github.com/amd/aocl-fftz/archive/refs/tags/${AOCL_FFTZ_VERSION}.tar.gz" \
    | tar -xz

cd "aocl-fftz-${AOCL_FFTZ_VERSION}"

cmake -B buildFFTZ
sudo cmake --build buildFFTZ --target install -j"$(nproc)"
```

Atualize o cache do carregador dinâmico:

```bash
sudo ldconfig
```

## 5. Instalação do OCUDU

Clone o repositório:

```bash
mkdir -p ~/src

git clone https://gitlab.com/ocudu/ocudu.git ~/src/ocudu
cd ~/src/ocudu
```

Configure a compilação:

```bash
cmake -S . -B build \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_DPDK=OFF \
    -DENABLE_UHD=OFF \
    -DENABLE_SIDEKIQ=OFF \
    -DBUILD_TESTING=OFF \
    -DASSERT_LEVEL=MINIMAL
```

Compile e instale:

```bash
cmake --build build -j"$(nproc)"
sudo cmake --install build
```

## 6. Habilitação do kernel Real-time

A habilitação do kernel Real-time requer uma assinatura válida do Ubuntu Pro.

```bash
sudo pro attach <CHAVE_UBUNTU_PRO>
sudo pro enable realtime-kernel --variant=generic --assume-yes
```

Reinicie o servidor após a instalação:

```bash
sudo reboot
```

Depois da reinicialização, verifique o kernel ativo:

```bash
uname -a
```

## 7. Configuração do pipx

```bash
pipx ensurepath
exec "$SHELL" -l
```

## 8. Configuração de desempenho com tuned

Crie o diretório do perfil:

```bash
sudo mkdir -p /usr/lib/tuned/srs
```

Baixe os arquivos de configuração:

```bash
sudo wget \
    -O /usr/lib/tuned/srs/startup.sh \
    https://docs.srsran.com/projects/project/en/latest/_downloads/cbd188942ee96dc818179209d3df29ab/startup.sh

sudo wget \
    -O /usr/lib/tuned/srs/tuned.conf \
    https://docs.srsran.com/projects/project/en/latest/_downloads/ff850d99a3e52a6d2acfb05670ff8fad/tuned.conf
```

Ajuste a permissão do script:

```bash
sudo chmod +x /usr/lib/tuned/srs/startup.sh
```

Ative o perfil:

```bash
sudo tuned-adm list
sudo tuned-adm profile srs
sudo tuned-adm active
sudo systemctl enable tuned.service
```

Reinicie o servidor:

```bash
sudo reboot
```

## 9. Desativação da memória swap

Desative a swap na sessão atual:

```bash
sudo swapoff -a
```

Para manter a swap desativada após reinicializações, edite `/etc/fstab` e comente a entrada correspondente:

```bash
sudo vim /etc/fstab
```

Exemplo:

```text
#/swap.img none swap sw 0 0
```

Confirme o estado:

```bash
swapon --show
free -h
```

## 10. Ajuste dos buffers de rede do kernel

Edite o arquivo:

```bash
sudo vim /etc/sysctl.conf
```

Adicione:

```text
net.core.wmem_max = 33554432
net.core.rmem_max = 33554432
net.core.wmem_default = 33554432
net.core.rmem_default = 33554432
```

Aplique as alterações:

```bash
sudo sysctl -p
```

## 11. Instalação do Poetry

```bash
pipx install poetry
pipx ensurepath
exec "$SHELL" -l
```

Verifique a instalação:

```bash
poetry --version
```

## 12. Instalação do ICARUS

Instalar o Solver Gurobi (https://www.gurobi.com/)

Clone o repositório:

```bash
git clone https://github.com/lindalvo/ICARUS.git ~/ICARUS
cd ~/ICARUS
```

Crie o arquivo local de configuração:

```bash
cp .env.example .env
```

Edite as variáveis de ambiente:

```bash
vim .env
```

Entre as variáveis utilizadas pelo projeto podem estar:

```dotenv
Filename=<identificador_da_instancia>
OUT_DIR=<diretorio_de_saida>
```

Consulte `.env.example` para a relação completa e os formatos esperados.

Instale as dependências Python:

```bash
poetry lock
poetry sync
```

Para instalar sem atualizar as versões já registradas no arquivo `poetry.lock`, normalmente basta:

```bash
poetry sync
```

## 13. Execução do pré-processamento

Execute o primeiro estágio do pipeline:

```bash
poetry run python ICARUS/src/01_filter_group_csv_ANATEL.py
poetry run python ICARUS/src/02_ilp_scenarios.py
poetry run python ICARUS/src/03_1_check_scenarios.py
poetry run python ICARUS/src/03_2_table_auxiliar.py
poetry run python ICARUS/src/03_3_maps.py
poetry run python ICARUS/src/03_4_stats.py
poetry run python ICARUS/src/04_generate_pipeline.py
```

O script bash `05_run_pipeline_ICARUS.sh ` deve ser executado no servidor ICARUS como root. Ele ler os dois arquivos pipelines gerados para cada cenário e para cada linha de cada arquivo, executar o `run_topology.sh ` que monta a topologia, coleta as métricas e as armazena no SQLLite através do script ` get_gnbemu_kpi.py ` , e desmonta a topologia.

Os scripts Bash devem receber permissão de execução quando necessário:

```bash
chmod +x caminho/do/script.sh
```

### Cenário otimizado

O cenário otimizado é produzido por um modelo ILP. A formulação considera restrições como:

- distância máxima entre O-RU e O-DU;
- capacidade agregada da O-DU;
- quantidade máxima de O-RUs por O-DU;
- ativação das localizações utilizadas como O-DUs;
- associação local da O-RU instalada na mesma posição da O-DU.

A otimização é realizada de forma lexicográfica. Primeiro, determina-se a menor quantidade viável de O-DUs. Em seguida, essa quantidade é fixada e o objetivo secundário do cenário é otimizado.

### Cenário estressado

O cenário adversarial mantém as condições estruturais necessárias para permitir comparação com o cenário otimizado, mas reorganiza deliberadamente as associações O-RU–O-DU para aumentar o estresse da topologia.

O estresse é introduzido principalmente por:

- maior desbalanceamento de largura de banda entre O-DUs;
- concentração de carga em determinados clusters;
- distribuição menos favorável dos recursos computacionais.

O cenário adversarial não representa uma alternativa mais eficiente. Ele funciona como um caso de estresse controlado, utilizado para avaliar a sensibilidade das métricas às alterações de posicionamento e associação.

## Pipeline experimental

O pipeline do ICARUS executa, em linhas gerais, as seguintes etapas:

1. leitura e tratamento dos dados da ANATEL;
2. consolidação das estações e larguras de banda;
3. cálculo das distâncias geográficas;
4. geração dos cenários otimizado e estressado;
5. exportação das associações O-RU–O-DU;
6. geração das configurações YAML;
7. criação das interfaces e namespaces de rede;
8. configuração dos atrasos de fronthaul;
9. inicialização das O-DUs e dos emuladores de O-RU;
10. coleta de métricas;
11. repetição das rodadas experimentais;
12. armazenamento dos resultados no banco SQLite;
13. geração de arquivos e mapas para análise.

## Banco de dados

As métricas coletadas são armazenadas em um banco de dados SQLite, possibilitando:

- consulta por cenário;
- consulta por O-DU ou cluster;
- comparação entre execuções;
- agregação de rodadas;
- análise de CPU, memória, energia e rede;
- exportação posterior para CSV ou ferramentas estatísticas.

O banco de dados e os demais artefatos são armazenados no diretório configurado pela variável `OUT_DIR`.

## Reprodutibilidade

Para reduzir interferências entre os cenários, recomenda-se:

- utilizar o mesmo hardware em todas as execuções;
- manter fixas as configurações da O-DU e dos emuladores;
- utilizar a mesma quantidade de UEs por O-RU;
- manter afinidade de CPU e NUMA;
- desabilitar serviços desnecessários;
- utilizar o mesmo perfil `tuned`;
- manter a swap desativada;
- utilizar o kernel Real-time;
- repetir as execuções;
- alternar ou controlar a ordem dos cenários;
- registrar as versões do sistema, kernel, OCUDU e dependências;
- evitar alterações no ambiente durante as coletas.

## Estrutura do repositório

A estrutura pode variar conforme a versão do projeto. De forma geral:

```text
ICARUS/
├── .env.example
├── pyproject.toml
├── poetry.lock
├── README.md
├── ICARUS/
│   └── src/
│       ├── 01_filter_group_csv_ANATEL.py
│       ├── ...
│       └── maps.py
└── OUT/
```

## Observações importantes

- Alguns comandos exigem privilégios administrativos.
- A desativação de serviços deve ser avaliada antes de ser aplicada em servidores de uso geral.
- O kernel Real-time depende do Ubuntu Pro.
- O OpenStreetMap e outros provedores externos podem ser utilizados na geração de mapas; recomenda-se manter cache local para garantir reprodutibilidade.
- As coordenadas de entrada devem estar em formato numérico válido.
- A distância geodesíca representa uma aproximação da extensão do enlace físico.
- O atraso de propagação não contempla necessariamente filas, comutação, sincronização ou processamento.
- As métricas devem ser interpretadas comparativamente entre cenários executados sob condições equivalentes.

## Licença

## Citação

## Autor

**Lindalvo Alcantara**

Projeto desenvolvido no contexto de pesquisa sobre estratégias de posicionamento e associação de O-DUs e O-RUs em arquiteturas Open RAN.
